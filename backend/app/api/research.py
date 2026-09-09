from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextvars import copy_context
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, inspect, or_, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import get_db
from app.models import (
    AgentRun,
    CapabilityAuditEvent,
    CapabilityConfig,
    CapabilityConfigRevision,
    CapabilityReview,
    CapabilityRun,
    CapabilityRunRevision,
    CapabilityRunTarget,
    CapabilitySchedule,
    CapabilityTemplate,
    Document,
    DocumentAbstractState,
    DocumentEntity,
    DocumentTopic,
    DocumentVersion,
    EventEntity,
    EventMention,
    EventRelation,
    EvidenceClaim,
    FieldMaterial,
    ProjectEvidence,
    ProjectTask,
    ResearchCase,
    ResearchCaseDataSlice,
    ResearchCaseDocument,
    ResearchCaseEvent,
    ResearchCaseMember,
    ResearchContribution,
    ResearchEntity,
    ResearchEvent,
    ResearchExport,
    Source,
    SourceChannel,
    SourceChannelPolicy,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
    StructuredSnapshot,
    StructuredSnapshotObservation,
    Topic,
    User,
)
from app.services import project_research as project_workflow
from app.services.agent_runtime import (
    AgentRequest,
    AgentRuntimeError,
    AnswerArtifact,
    AnswerClaim,
    AnswerSection,
    EvidenceItem,
    FactItem,
    ToolResult,
)
from app.services.country_agent import (
    RegisteredAgentTool,
    artifact_markdown,
    run_country_agent,
)
from app.services.country_data_catalog import country_data_catalog
from app.services.field_materials import (
    FieldMaterialValidationError,
    resolve_field_material_path,
    store_field_material,
)
from app.services.live_search import (
    CodexLocalLiveSearchProvider,
    DisabledLiveSearchProvider,
    LiveSearchProvider,
    OpenAIResponsesLiveSearchProvider,
)
from app.services.reader_access import (
    ROLE_PERMISSIONS,
    ReaderAccessDenied,
    authenticate_reader_key,
    ensure_owner_memberships,
    issue_reader_key,
    public_member_payload,
    reader_keys_initialized,
    record_contribution,
    require_case_permission,
)
from app.services.research_capabilities import ResearchCapabilityError, run_capability

router = APIRouter(prefix="/api/v1/reader", tags=["research-reader"])
DbSession = Annotated[Session, Depends(get_db)]
DRC_MVP_HARVEST_PATH = Path(__file__).resolve().parents[3] / "data" / "drc_mvp_harvest.json"
COUNTRY_CATALOG_PATH = Path(__file__).resolve().parents[3] / "data" / "country_catalog.json"
COUNTRY_BASIC_FACTS_PATH = Path(__file__).resolve().parents[3] / "data" / "country_basic_facts.json"
COUNTRY_SPACE_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "data" / "country_space_templates.json"
INDICATOR_CATALOG_PATH = Path(__file__).resolve().parents[3] / "data" / "indicator_catalog.json"
SOURCE_PROFILE_PATH = Path(__file__).resolve().parents[3] / "data" / "source_profiles.json"
MAP_ASSET_MANIFEST_PATH = Path(__file__).resolve().parents[3] / "data" / "map_assets_manifest.json"
READER_APP_VERSION = "reader-research-agent-v2"
ASSISTANT_STREAM_HEARTBEAT_SECONDS = 1.5
ASSISTANT_STREAM_QUEUE_SIZE = 128
_ASSISTANT_STREAM_TERMINAL_EVENTS = {"run.completed", "run.failed"}
_ASSISTANT_RUN_PROGRESS: dict[int, dict[str, Any]] = {}
_ASSISTANT_RUN_PROGRESS_LOCK = Lock()
_ASSISTANT_CANCEL_REQUESTS: set[int] = set()


_STANDARD_INDICATORS = (
    {
        "key": "population",
        "label": "总人口",
        "dataset_key": "world-bank-indicators-v2",
        "indicator_code": "SP.POP.TOTL",
        "metric_code": "population_total",
        "source_name": "World Bank",
    },
    {
        "key": "gdp",
        "label": "GDP",
        "dataset_key": "world-bank-indicators-v2",
        "indicator_code": "NY.GDP.MKTP.CD",
        "metric_code": "gdp_current_usd",
        "source_name": "World Bank",
    },
    {
        "key": "gdp_growth",
        "label": "GDP 增速",
        "dataset_key": "world-bank-indicators-v2",
        "indicator_code": "NY.GDP.MKTP.KD.ZG",
        "metric_code": "gdp_real_growth",
        "source_name": "World Bank",
    },
    {
        "key": "government_debt",
        "label": "政府债务占 GDP",
        "dataset_key": "imf-weo-government-debt",
        "indicator_code": "GGXWDG_NGDP",
        "metric_code": "general_government_gross_debt_percent_gdp",
        "source_name": "IMF WEO",
    },
    {
        "key": "oda",
        "label": "ODA 援助",
        "dataset_key": "oecd-dac2a-oda-disbursements",
        "indicator_code": "DAC2A.206.ALLD",
        "metric_code": "oda_disbursements_current_usd_millions",
        "source_name": "OECD DAC",
    },
    {
        "key": "fdi_inward",
        "label": "FDI 流入",
        "dataset_key": "unctad-fdi-flows-stock",
        "indicator_code": "UNCTAD.US.FdiFlowsStock.08.1",
        "metric_code": "fdi_inward_flow_current_usd_millions",
        "source_name": "UNCTAD",
    },
)

_COUNTRY_INDICATORS = {
    "COD": _STANDARD_INDICATORS,
    "ZAF": _STANDARD_INDICATORS,
    "ZMB": _STANDARD_INDICATORS,
    "ZWE": _STANDARD_INDICATORS,
}

_COUNTRY_DISPLAY = {
    "COD": {
        "name_zh": "刚果民主共和国（刚果（金））",
        "name_en": "Democratic Republic of the Congo",
    }
}

_COUNTRY_MODULES = (
    ("overview", "国别总览"),
    ("administrative-map", "行政区地图"),
    ("macro", "六项核心宏观指标"),
    ("trends", "十年时序趋势"),
    ("minerals", "资源与矿业"),
    ("trade", "对外贸易与供应链"),
    ("policy", "政策法规与动态"),
    ("events", "事件与安全"),
    ("sources", "来源底账与数据新鲜度"),
    ("assistant", "AI 研判与能力生成"),
)


@router.get("/bootstrap")
def get_reader_bootstrap(db: DbSession) -> dict[str, Any]:
    country_count = len(_country_catalog()["countries"])
    settings = get_settings()
    base = {
        "app_version": READER_APP_VERSION,
        "reader_contract_version": 2,
        "api_ready": True,
        "database_ready": False,
        "schema_ready": False,
        "demo_seeded": False,
        "country_count": country_count,
        "event_count": 0,
        "topic_count": 0,
        "capability_count": 0,
        "source_count": 0,
        "source_channel_count": 0,
        "failed_source_count": 0,
        "latest_collection_success_at": None,
        "source_families": [],
        "dataset_count": 0,
        "reviewed_material_count": 0,
        "capability_runs_ready": 0,
        "positive_demo_questions_ready": False,
        "seed_version": "mvp-reader-rescue-v2",
        "database_target": None,
        "map_public_status": "unknown",
        "assistant_runtime": settings.agent_runtime,
        "assistant_model_enabled": settings.agent_runtime != "evidence_only",
        "live_search_provider": settings.live_search_provider,
        "migration_version": None,
        "problems": [],
    }
    try:
        db.execute(text("select 1"))
        base["database_ready"] = True
        inspector = inspect(db.get_bind())
        tables = set(inspector.get_table_names())
    except SQLAlchemyError:
        base["problems"] = [
            {
                "code": "database_unavailable",
                "message": "数据库不可用；请检查 GUOBIE_DATABASE_URL 与 PostgreSQL 服务。",
            }
        ]
        return base

    required_tables = {
        "sources",
        "structured_datasets",
        "research_cases",
        "events",
        "capability_configs",
        "capability_run_targets",
        "field_materials",
    }
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        base["problems"] = [
            {
                "code": "schema_not_ready",
                "message": f"数据库迁移未完成，缺少表：{', '.join(missing_tables)}。",
            }
        ]
        return base

    base["schema_ready"] = True
    bind_url = db.get_bind().url
    base["database_target"] = f"{bind_url.drivername}:{bind_url.database or 'memory'}"
    try:
        manifest = json.loads(MAP_ASSET_MANIFEST_PATH.read_text(encoding="utf-8"))
        base["map_public_status"] = manifest.get("public_release_status", "unknown")
    except (OSError, json.JSONDecodeError):
        base["map_public_status"] = "manifest_unavailable"
    if "alembic_version" in tables:
        base["migration_version"] = db.scalar(text("select version_num from alembic_version"))

    try:
        base["event_count"] = (
            db.scalar(
                select(func.count())
                .select_from(ResearchEvent)
                .where(ResearchEvent.review_status == "reviewed")
            )
            or 0
        )
        base["topic_count"] = (
            db.scalar(select(func.count()).select_from(ResearchCase).where(ResearchCase.status == "active"))
            or 0
        )
        active_capabilities = db.execute(
            select(CapabilityConfig, CapabilityTemplate)
            .join(CapabilityTemplate, CapabilityTemplate.id == CapabilityConfig.template_id)
            .where(
                CapabilityConfig.status == "active",
                CapabilityTemplate.status == "active",
            )
        ).all()
        base["capability_count"] = sum(
            1
            for _config, template in active_capabilities
            if template.default_config.get("catalog_visibility", "skill_catalog") == "skill_catalog"
        )
        base["source_count"] = db.scalar(select(func.count()).select_from(Source)) or 0
        if "source_channels" in tables:
            base["source_channel_count"] = db.scalar(select(func.count()).select_from(SourceChannel)) or 0
            base["failed_source_count"] = (
                db.scalar(
                    select(func.count()).select_from(SourceChannel).where(SourceChannel.status == "blocked")
                )
                or 0
            )
            base["latest_collection_success_at"] = db.scalar(select(func.max(SourceChannel.last_success_at)))
            family_rows = db.execute(
                select(
                    Source.source_type,
                    func.count(func.distinct(Source.id)),
                    func.max(SourceChannel.last_success_at),
                )
                .select_from(Source)
                .outerjoin(SourceChannel, SourceChannel.source_id == Source.id)
                .group_by(Source.source_type)
                .order_by(Source.source_type)
            ).all()
            base["source_families"] = [
                {
                    "source_type": source_type,
                    "source_count": source_count,
                    "last_success_at": last_success_at,
                }
                for source_type, source_count, last_success_at in family_rows
            ]
        base["dataset_count"] = db.scalar(select(func.count()).select_from(StructuredDataset)) or 0
        base["reviewed_material_count"] = (
            db.scalar(
                select(func.count(func.distinct(DocumentEntity.document_version_id))).where(
                    DocumentEntity.review_status == "confirmed"
                )
            )
            or 0
        )
        templates = {
            row.id: row.slug
            for row in db.scalars(select(CapabilityTemplate).where(CapabilityTemplate.status == "active"))
        }
        configs = {row.id: row.template_id for row in db.scalars(select(CapabilityConfig))}
        ready_config_ids = {
            run.config_id
            for run in db.scalars(select(CapabilityRun).where(CapabilityRun.status == "succeeded"))
            if _capability_output_has_evidence(templates.get(configs.get(run.config_id)), run.output)
        }
        base["capability_runs_ready"] = len(ready_config_ids)
        positive_questions = set()
        expected_capabilities = {
            _QA_PRESETS[0]["question"]: "country_snapshot",
            _QA_PRESETS[1]["question"]: "policy_timeline",
            _QA_PRESETS[2]["question"]: "event_evidence_compare",
        }
        for run in db.scalars(
            select(AgentRun).where(
                AgentRun.country_iso3 == "COD",
                AgentRun.status == "succeeded",
            )
        ):
            citations = (run.artifact or {}).get("citations") or []
            expected = expected_capabilities.get(run.question)
            matching_trace = next(
                (
                    item
                    for item in run.tool_trace or []
                    if item.get("capability") == expected and item.get("status") == "succeeded"
                ),
                None,
            )
            if (
                expected
                and matching_trace
                and matching_trace.get("evidence_count", 0) >= 2
                and len(citations) >= 2
            ):
                positive_questions.add(run.question)
        base["positive_demo_questions_ready"] = len(positive_questions) == 3
    except SQLAlchemyError:
        base["schema_ready"] = False
        base["problems"] = [
            {
                "code": "schema_query_failed",
                "message": "数据库表已存在，但 Reader 启动查询失败；请复核迁移版本。",
            }
        ]
        return base

    base["demo_seeded"] = all(
        (
            base["event_count"] >= 2,
            base["topic_count"] >= 1,
            base["reviewed_material_count"] >= 8,
            base["capability_count"] >= 3,
            base["capability_runs_ready"] >= 3,
            base["positive_demo_questions_ready"],
        )
    )
    if not base["demo_seeded"]:
        base["problems"] = [
            {
                "code": "demo_not_seeded",
                "message": (
                    "演示数据未达到最低门槛：2 个 reviewed 事件、8 份复核材料、"
                    "1 个专题、3 个非空能力产物、3 个可引用正向问题。"
                ),
            }
        ]
    return base


_QA_PRESETS = (
    {"id": "q1", "label": "宏观底账", "question": "刚果（金）当前已入库的六项宏观指标是什么？"},
    {"id": "q2", "label": "政策时间线", "question": "当前有哪些已复核政策材料及其发布时间？"},
    {"id": "q3", "label": "事件证据", "question": "当前已复核事件的不同来源主张有哪些差异？"},
    {"id": "q4", "label": "数据新鲜度", "question": "哪些数据已经过期或缺少精确发布时间？"},
)

_PROFILE_SIGNAL_LABELS = {
    "capital": "首都",
    "currency": "货币",
    "official_languages": "官方语言",
    "land_area": "国土面积",
    "policy_material": "已确认政策材料",
    "reviewed_event": "已复核事件",
    "structured_dataset": "结构化数据集",
    "gdp_observation": "GDP观测",
    "population_observation": "人口观测",
    "commodity_material": "资源或矿产材料",
    "environment_observation": "资源环境观测",
    "trade_dataset": "贸易或跨境数据集",
    "cross_border_event": "跨境关联事件",
}
_MANUAL_PERSPECTIVE_GROUPS = {
    "government",
    "local_media",
    "international_organization",
    "academic",
    "enterprise",
    "social_media",
}
_UNIFIED_OBJECT_TYPES = {"all", "country", "event", "policy", "report", "paper", "data"}


def _country_catalog() -> dict[str, Any]:
    try:
        payload = json.loads(COUNTRY_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="country catalog is unavailable") from exc
    countries = payload.get("countries")
    if not isinstance(countries, list) or len(countries) != 195:
        raise HTTPException(status_code=503, detail="country catalog failed validation")
    return payload


def _country_basic_facts() -> dict[str, Any]:
    try:
        payload = json.loads(COUNTRY_BASIC_FACTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="country basic facts are unavailable") from exc
    countries = payload.get("countries")
    catalog_iso3 = {item["iso3"] for item in _country_catalog()["countries"]}
    if not isinstance(countries, dict) or set(countries) != catalog_iso3:
        raise HTTPException(status_code=503, detail="country basic facts failed coverage validation")
    for iso3, item in countries.items():
        if (
            not isinstance(item.get("capital"), list)
            or not item["capital"]
            or not isinstance(item.get("currencies"), list)
            or not item["currencies"]
            or not isinstance(item.get("official_languages"), list)
            or not item["official_languages"]
            or not isinstance(item.get("land_area_km2"), (int, float))
            or item["land_area_km2"] <= 0
        ):
            raise HTTPException(
                status_code=503,
                detail=f"country basic facts are incomplete for {iso3}",
            )
    return payload


def _country_basic_fact_profile(iso3: str) -> dict[str, Any]:
    payload = _country_basic_facts()
    item = payload["countries"][iso3]
    currencies = [f"{currency['name']}（{currency['code']}）" for currency in item["currencies"]]
    values = {
        "capital": "、".join(item["capital"]),
        "currency": "、".join(currencies),
        "official_languages": "、".join(language["name"] for language in item["official_languages"]),
        "land_area": f"{item['land_area_km2']:,.2f}".rstrip("0").rstrip(".") + " 平方公里",
    }
    provenance = {
        **payload["source"],
        "retrieved_at": payload["retrieved_at"],
        "field_override": payload.get("field_overrides", {}).get(iso3),
    }
    return {"values": values, "details": item, "source": provenance}


def _require_catalog_country(iso3: str) -> None:
    if iso3 not in {item["iso3"] for item in _country_catalog()["countries"]}:
        raise HTTPException(status_code=422, detail="country is not in the UN M49 catalog")


def _country_space_profile(iso3: str, catalog_item: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(COUNTRY_SPACE_TEMPLATE_PATH.read_text(encoding="utf-8"))
        base = payload["base"]
        overrides = payload.get("country_overrides", {})
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="country space template is unavailable") from exc
    override = overrides.get(iso3, {})
    label_overrides = override.get("module_labels", {})
    modules = [
        {**module, "label": label_overrides.get(module["key"], module["label"])}
        for module in base.get("modules", [])
    ]
    qa_templates = override.get("qa_presets") or base.get("qa_presets", [])
    qa_presets = [
        {
            **item,
            "question": item["question"].format(
                country_name=catalog_item["name_zh"],
                iso3=iso3,
            ),
        }
        for item in qa_templates
    ]
    return {
        "template_key": base["template_key"],
        "profile_key": override.get("profile_key", base["profile_key"]),
        "profile_description": override.get(
            "profile_description",
            base.get("profile_description", ""),
        ),
        "preview_enabled": bool(override.get("preview_enabled", base.get("preview_enabled", False))),
        "focus_tags": override.get("focus_tags", base.get("focus_tags", [])),
        "extension_rule": base.get("extension_rule", {}),
        "basic_fact_slots": base.get("basic_fact_slots", []),
        "indicator_slots": base.get("indicator_slots", []),
        "profile_dimensions": base.get("profile_dimensions", []),
        "modules": modules,
        "extension_topics": override.get(
            "extension_topics",
            base.get("extension_topics", []),
        ),
        "qa_presets": qa_presets,
    }


def _latest_value(*values: datetime | None) -> datetime | None:
    normalized = [value for value in values if value is not None]
    return max(normalized) if normalized else None


def _is_recent(value: datetime | None, *, days: int = 90) -> bool:
    if value is None:
        return False
    comparable = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return comparable >= datetime.now(UTC) - timedelta(days=days)


def _country_coverage_maps(db: Session) -> dict[str, Any]:
    material_counts: dict[str, int] = {}
    material_updates: dict[str, datetime | None] = {}
    material_breakdown: dict[str, dict[tuple[str, str], int]] = defaultdict(dict)
    for iso3, document_type, source_type, count, updated_at in db.execute(
        select(
            ResearchEntity.canonical_key,
            Document.document_type,
            Source.source_type,
            func.count(func.distinct(Document.id)),
            func.max(Document.last_seen_at),
        )
        .join(DocumentEntity, DocumentEntity.entity_id == ResearchEntity.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentEntity.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            ResearchEntity.entity_type == "country",
            DocumentEntity.review_status == "confirmed",
        )
        .group_by(ResearchEntity.canonical_key, Document.document_type, Source.source_type)
    ):
        material_breakdown[iso3][(document_type, source_type)] = int(count or 0)
        material_counts[iso3] = material_counts.get(iso3, 0) + int(count or 0)
        material_updates[iso3] = _latest_value(material_updates.get(iso3), updated_at)

    event_counts: dict[str, int] = {}
    event_updates: dict[str, datetime | None] = {}
    for iso3, count, updated_at in db.execute(
        select(
            ResearchEntity.canonical_key,
            func.count(ResearchEvent.id),
            func.max(ResearchEvent.updated_at),
        )
        .join(ResearchEvent, ResearchEvent.country_entity_id == ResearchEntity.id)
        .where(
            ResearchEntity.entity_type == "country",
            ResearchEvent.review_status == "reviewed",
        )
        .group_by(ResearchEntity.canonical_key)
    ):
        event_counts[iso3] = int(count or 0)
        event_updates[iso3] = updated_at

    observation_counts: dict[str, int] = {}
    dataset_counts: dict[str, int] = {}
    observation_updates: dict[str, datetime | None] = {}
    indicator_codes: dict[str, set[str]] = defaultdict(set)
    dataset_keys: dict[str, set[str]] = defaultdict(set)
    for iso3, dataset_key, indicator_code, count, updated_at in db.execute(
        select(
            StructuredObservation.country_iso3,
            StructuredDataset.dataset_key,
            StructuredObservation.indicator_code,
            func.count(StructuredObservation.id),
            func.max(StructuredObservation.last_seen_at),
        )
        .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
        .group_by(
            StructuredObservation.country_iso3,
            StructuredDataset.dataset_key,
            StructuredObservation.indicator_code,
        )
    ):
        observation_counts[iso3] = observation_counts.get(iso3, 0) + int(count or 0)
        dataset_keys[iso3].add(dataset_key)
        dataset_counts[iso3] = len(dataset_keys[iso3])
        if indicator_code:
            indicator_codes[iso3].add(indicator_code)
        observation_updates[iso3] = _latest_value(observation_updates.get(iso3), updated_at)

    pending_counts: dict[str, int] = defaultdict(int)
    for iso3, count in db.execute(
        select(ResearchEntity.canonical_key, func.count(DocumentEntity.document_version_id))
        .join(DocumentEntity, DocumentEntity.entity_id == ResearchEntity.id)
        .where(
            ResearchEntity.entity_type == "country",
            DocumentEntity.review_status == "pending",
        )
        .group_by(ResearchEntity.canonical_key)
    ):
        pending_counts[iso3] += int(count or 0)
    for iso3, count in db.execute(
        select(ResearchEntity.canonical_key, func.count(EventMention.id))
        .join(ResearchEvent, ResearchEvent.country_entity_id == ResearchEntity.id)
        .join(EventMention, EventMention.event_id == ResearchEvent.id)
        .where(
            ResearchEntity.entity_type == "country",
            EventMention.review_status == "pending",
        )
        .group_by(ResearchEntity.canonical_key)
    ):
        pending_counts[iso3] += int(count or 0)
    for iso3, count in db.execute(
        select(ResearchEntity.canonical_key, func.count(ResearchEvent.id))
        .join(ResearchEvent, ResearchEvent.country_entity_id == ResearchEntity.id)
        .where(
            ResearchEntity.entity_type == "country",
            ResearchEvent.review_status == "draft",
        )
        .group_by(ResearchEntity.canonical_key)
    ):
        pending_counts[iso3] += int(count or 0)

    commodity_versions = (
        select(DocumentEntity.document_version_id)
        .join(ResearchEntity, ResearchEntity.id == DocumentEntity.entity_id)
        .where(
            ResearchEntity.entity_type == "commodity",
            DocumentEntity.review_status == "confirmed",
        )
    )
    commodity_materials = {
        iso3: int(count or 0)
        for iso3, count in db.execute(
            select(
                ResearchEntity.canonical_key,
                func.count(func.distinct(DocumentVersion.document_id)),
            )
            .join(DocumentEntity, DocumentEntity.entity_id == ResearchEntity.id)
            .join(DocumentVersion, DocumentVersion.id == DocumentEntity.document_version_id)
            .where(
                ResearchEntity.entity_type == "country",
                DocumentEntity.review_status == "confirmed",
                DocumentEntity.document_version_id.in_(commodity_versions),
            )
            .group_by(ResearchEntity.canonical_key)
        )
    }
    cross_border_events = {
        iso3: int(count or 0)
        for iso3, count in db.execute(
            select(
                ResearchEntity.canonical_key,
                func.count(func.distinct(ResearchEvent.id)),
            )
            .join(ResearchEvent, ResearchEvent.country_entity_id == ResearchEntity.id)
            .join(EventEntity, EventEntity.event_id == ResearchEvent.id)
            .where(
                ResearchEntity.entity_type == "country",
                ResearchEvent.review_status == "reviewed",
                EventEntity.role == "affected_country",
            )
            .group_by(ResearchEntity.canonical_key)
        )
    }
    return {
        "material_counts": material_counts,
        "material_updates": material_updates,
        "material_breakdown": material_breakdown,
        "event_counts": event_counts,
        "event_updates": event_updates,
        "observation_counts": observation_counts,
        "dataset_counts": dataset_counts,
        "observation_updates": observation_updates,
        "indicator_codes": indicator_codes,
        "dataset_keys": dataset_keys,
        "pending_counts": pending_counts,
        "commodity_materials": commodity_materials,
        "cross_border_events": cross_border_events,
    }


def _country_profile_completeness(
    iso3: str,
    basic_facts: dict[str, str],
    profile: dict[str, Any],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    breakdown = coverage["material_breakdown"].get(iso3, {})
    policy_materials = sum(
        count
        for (document_type, _), count in breakdown.items()
        if document_type in {"policy", "policy_document"}
    )
    dataset_keys = coverage["dataset_keys"].get(iso3, set())
    indicator_codes = coverage["indicator_codes"].get(iso3, set())
    signals = {
        "capital": bool(basic_facts.get("capital")),
        "currency": bool(basic_facts.get("currency")),
        "official_languages": bool(basic_facts.get("official_languages")),
        "land_area": bool(basic_facts.get("land_area")),
        "policy_material": policy_materials > 0,
        "reviewed_event": coverage["event_counts"].get(iso3, 0) > 0,
        "structured_dataset": coverage["dataset_counts"].get(iso3, 0) > 0,
        "gdp_observation": any(code.startswith("NY.GDP") for code in indicator_codes),
        "population_observation": "SP.POP.TOTL" in indicator_codes,
        "commodity_material": coverage["commodity_materials"].get(iso3, 0) > 0,
        "environment_observation": any(code.startswith(("EN.", "AG.", "EG.")) for code in indicator_codes),
        "trade_dataset": any(
            token in key.lower() for key in dataset_keys for token in ("trade", "comtrade", "wits", "fdi")
        ),
        "cross_border_event": coverage["cross_border_events"].get(iso3, 0) > 0,
    }
    dimensions = []
    for dimension in profile.get("profile_dimensions", []):
        slots = dimension.get("slots", [])
        filled_slots = [slot for slot in slots if signals.get(slot, False)]
        missing_slots = [slot for slot in slots if not signals.get(slot, False)]
        total = len(slots)
        filled = len(filled_slots)
        dimensions.append(
            {
                "key": dimension["key"],
                "label": dimension["label"],
                "filled": filled,
                "total": total,
                "percent": round(filled * 100 / total) if total else 0,
                "status": "complete" if total and filled == total else "partial" if filled else "gap",
                "missing": [_PROFILE_SIGNAL_LABELS.get(slot, slot) for slot in missing_slots],
            }
        )
    filled_total = sum(item["filled"] for item in dimensions)
    slot_total = sum(item["total"] for item in dimensions)
    percent = round(filled_total * 100 / slot_total) if slot_total else 0
    has_evidence = any(
        (
            coverage["material_counts"].get(iso3, 0),
            coverage["event_counts"].get(iso3, 0),
            coverage["observation_counts"].get(iso3, 0),
        )
    )
    quality = "deep" if has_evidence and percent >= 60 else "partial" if has_evidence else "gap"
    return {
        "dimensions": dimensions,
        "filled": filled_total,
        "total": slot_total,
        "percent": percent,
        "quality": quality,
        "gap_reasons": [
            f"{item['label']}：缺少{'、'.join(item['missing'])}" for item in dimensions if item["missing"]
        ],
    }


def _load_optional_registry(path: Path, *, key: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get(key, [])
    if not isinstance(rows, list):
        return {}
    return {str(item.get("key", "")).strip(): item for item in rows if item.get("key")}


def _source_profile(source: Source) -> dict[str, Any]:
    registry = _load_optional_registry(SOURCE_PROFILE_PATH, key="profiles")
    item = registry.get(str(source.id)) or registry.get(source.name)
    return {
        "status": "reviewed" if item else "pending_verification",
        "organization": (item or {}).get("organization") or source.organization_name or source.name,
        "facts": (item or {}).get("facts", []),
        "country_or_region": source.country_or_region,
        "organization_type": (item or {}).get("organization_type"),
        "funding_background": (item or {}).get("funding_background"),
        "stance_labels": (item or {}).get("stance_labels", []),
        "historical_topics": (item or {}).get("historical_topics", []),
        "evidence": (item or {}).get("evidence", []),
        "method_note": (
            "机构资料逐项绑定官网依据与核对日期，不推断立场。"
            if item
            else "机构属性尚未完成人工复核，不推断资金背景或立场。"
        ),
    }


def _country_conflict_summary(db: Session, country_id: int | None) -> list[dict[str, Any]]:
    if country_id is None:
        return []
    rows = list(
        db.execute(
            select(EvidenceClaim, ResearchEvent)
            .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
            .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
            .where(
                ResearchEvent.country_entity_id == country_id,
                ResearchEvent.review_status == "reviewed",
                EventMention.review_status == "confirmed",
                EvidenceClaim.review_status == "confirmed",
            )
            .order_by(ResearchEvent.start_at.desc(), EvidenceClaim.comparison_key)
        )
    )
    groups: dict[tuple[int, str], list[EvidenceClaim]] = defaultdict(list)
    events: dict[int, ResearchEvent] = {}
    for claim, event in rows:
        groups[(event.id, claim.comparison_key)].append(claim)
        events[event.id] = event
    result = []
    for (event_id, comparison_key), claims in groups.items():
        values = {(claim.value_text, _number(claim.numeric_value), claim.unit) for claim in claims}
        if len(values) <= 1:
            continue
        result.append(
            {
                "event_id": event_id,
                "event_title": events[event_id].title,
                "comparison_key": comparison_key,
                "claim_count": len(claims),
                "values": [
                    {
                        "claimant": claim.claimant_name or "未登记主体",
                        "value": claim.value_text,
                        "numeric_value": _number(claim.numeric_value),
                        "unit": claim.unit,
                    }
                    for claim in claims
                ],
                "method_note": "相互冲突的主张并列展示，不按数量裁决。",
            }
        )
    return result[:6]


def _country_recent_changes(
    events: list[ResearchEvent],
    materials: list[tuple[DocumentVersion, Document, Source]],
    datasets: list[tuple[int, str, str, int]],
    structured_updated_at: datetime | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "object_type": "event",
            "object_key": f"event:{event.id}",
            "title": event.title,
            "changed_at": event.updated_at or event.start_at,
            "evidence_status": "reviewed",
            "route": f"events/{event.id}",
        }
        for event in events
    ]
    rows.extend(
        {
            "object_type": "material",
            "object_key": f"document-version:{version.id}",
            "title": version.title,
            "changed_at": document.last_seen_at,
            "evidence_status": "confirmed",
            "source": source.name,
        }
        for version, document, source in materials
    )
    if structured_updated_at:
        rows.extend(
            {
                "object_type": "data",
                "object_key": f"dataset:{dataset_id}",
                "title": name,
                "changed_at": structured_updated_at,
                "evidence_status": "snapshot_pinned",
                "observation_count": count,
            }
            for dataset_id, _, name, count in datasets
        )
    rows.sort(
        key=lambda item: (
            item["changed_at"] is not None,
            (
                item["changed_at"]
                if item["changed_at"] and item["changed_at"].tzinfo is not None
                else item["changed_at"].replace(tzinfo=UTC)
                if item["changed_at"]
                else datetime.min.replace(tzinfo=UTC)
            ),
        ),
        reverse=True,
    )
    return rows[:10]


@router.get("/countries/catalog")
def get_country_catalog(db: DbSession) -> dict[str, Any]:
    payload = _country_catalog()
    countries = payload["countries"]
    coverage = _country_coverage_maps(db)
    observation_counts = coverage["observation_counts"]

    def availability(item: dict[str, Any]) -> str:
        if item["availability"] == "deep_ready":
            return "deep_ready"
        if observation_counts.get(item["iso3"], 0):
            return "data_partial"
        return "framework_only"

    catalog_rows = []
    for item in countries:
        profile = _country_space_profile(item["iso3"], item)
        basic_facts = _country_basic_fact_profile(item["iso3"])
        completeness = _country_profile_completeness(item["iso3"], basic_facts["values"], profile, coverage)
        latest_evidence_at = _latest_value(
            coverage["material_updates"].get(item["iso3"]),
            coverage["event_updates"].get(item["iso3"]),
            coverage["observation_updates"].get(item["iso3"]),
        )
        catalog_rows.append(
            {
                **item,
                "availability": availability(item),
                "profile_layer_ready": True,
                "coverage_quality": completeness["quality"],
                "profile_completeness": {
                    "filled": completeness["filled"],
                    "total": completeness["total"],
                    "percent": completeness["percent"],
                },
                "profile_dimensions": completeness["dimensions"],
                "last_evidence_at": latest_evidence_at,
                "gap_reasons": completeness["gap_reasons"],
                "pending_verification_count": coverage["pending_counts"].get(item["iso3"], 0),
                "coverage": {
                    "basic_facts": len(basic_facts["values"]),
                    "structured_observations": observation_counts.get(item["iso3"], 0),
                    "structured_datasets": coverage["dataset_counts"].get(item["iso3"], 0),
                    "confirmed_materials": coverage["material_counts"].get(item["iso3"], 0),
                    "reviewed_events": coverage["event_counts"].get(item["iso3"], 0),
                },
                "template": {
                    key: value
                    for key, value in profile.items()
                    if key
                    in {
                        "template_key",
                        "profile_key",
                        "profile_description",
                        "preview_enabled",
                        "focus_tags",
                    }
                }
                | {"extension_topic_count": len(profile["extension_topics"])},
            }
        )
    source_count = db.scalar(select(func.count()).select_from(Source)) or 0
    dataset_count = db.scalar(select(func.count()).select_from(StructuredDataset)) or 0
    latest_collection = db.scalar(select(func.max(StructuredSnapshot.retrieved_at)))
    covered_count = sum(item["coverage_quality"] != "gap" for item in catalog_rows)
    return {
        "source": payload["source"],
        "coverage_summary": {
            "covered_countries": covered_count,
            "gap_countries": len(countries) - covered_count,
            "recently_updated_countries": sum(_is_recent(item["last_evidence_at"]) for item in catalog_rows),
            "pending_verification_countries": sum(
                item["pending_verification_count"] > 0 for item in catalog_rows
            ),
            "recent_window_days": 90,
        },
        "summary": {
            "country_count": len(countries),
            "profile_ready_count": sum(item["profile_layer_ready"] for item in catalog_rows),
            "deep_ready_count": sum(item["availability"] == "deep_ready" for item in catalog_rows),
            "data_partial_count": sum(item["availability"] == "data_partial" for item in catalog_rows),
            "template_preview_count": sum(item["template"]["preview_enabled"] for item in catalog_rows),
            "specialized_template_count": sum(
                item["template"]["extension_topic_count"] > 0 for item in catalog_rows
            ),
            "source_count": source_count,
            "dataset_count": dataset_count,
            "latest_collection_at": latest_collection,
        },
        "countries": catalog_rows,
    }


@router.get("/countries/{iso3}")
def get_country_space(iso3: str, db: DbSession) -> dict[str, Any]:
    normalized = iso3.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise HTTPException(status_code=422, detail="iso3 must have three letters")
    catalog_item = next(
        (item for item in _country_catalog()["countries"] if item["iso3"] == normalized),
        None,
    )
    if catalog_item is None:
        raise HTTPException(status_code=404, detail="country not found in the UN M49 catalog")
    country_profile = _country_space_profile(normalized, catalog_item)
    basic_fact_profile = _country_basic_fact_profile(normalized)
    coverage_maps = _country_coverage_maps(db)
    profile_completeness = _country_profile_completeness(
        normalized,
        basic_fact_profile["values"],
        country_profile,
        coverage_maps,
    )
    observations = (
        db.scalar(
            select(func.count())
            .select_from(StructuredObservation)
            .where(StructuredObservation.country_iso3 == normalized)
        )
        or 0
    )
    availability = (
        "deep_ready"
        if catalog_item["availability"] == "deep_ready"
        else "data_partial"
        if observations
        else "framework_only"
    )
    if availability == "framework_only":
        return {
            **catalog_item,
            "availability": availability,
            "coverage_level": "basic_profile",
            "template_preview_ready": country_profile["preview_enabled"],
            "profile_layer_ready": True,
            "name": catalog_item["name_zh"],
            "updated_at": basic_fact_profile["source"]["retrieved_at"],
            "basic_facts": basic_fact_profile["values"],
            "basic_fact_details": basic_fact_profile["details"],
            "basic_facts_source": basic_fact_profile["source"],
            "evidence_layer_ready": False,
            "coverage_quality": profile_completeness["quality"],
            "profile_completeness": {
                "filled": profile_completeness["filled"],
                "total": profile_completeness["total"],
                "percent": profile_completeness["percent"],
                "dimensions": profile_completeness["dimensions"],
                "method_note": "完整度只表示登记槽位覆盖，不是国家质量或可信度评分。",
            },
            "recent_changes": [],
            "source_mix": [],
            "conflict_summary": [],
            "map": {
                "current_level": None,
                "available_levels": [],
                "future_levels": ["ADM2", "ADM3"],
                "future_levels_status": "requires_source_license_and_boundary_review",
            },
            "summary": {
                "events": 0,
                "materials": 0,
                "structured_observations": 0,
                "structured_datasets": 0,
            },
            "modules": [
                {
                    **module,
                    "status": (
                        "available"
                        if module["key"] in {"overview", "sources", "assistant"}
                        else "not_connected"
                    ),
                    "freshness": (
                        basic_fact_profile["source"]["retrieved_at"]
                        if module["key"] in {"overview", "sources", "assistant"}
                        else None
                    ),
                }
                for module in country_profile["modules"]
            ],
            "country_template": country_profile,
            "key_indicators": [],
            "policy_items": [],
            "events": [],
            "materials": [],
            "datasets": [],
            "qa_presets": country_profile["qa_presets"],
        }
    evidence_schema_ready = inspect(db.get_bind()).has_table(ResearchEntity.__tablename__)
    country = None
    if evidence_schema_ready:
        country = db.scalar(
            select(ResearchEntity).where(
                ResearchEntity.entity_type == "country",
                or_(
                    ResearchEntity.canonical_key == normalized,
                    ResearchEntity.details["iso3"].as_string() == normalized,
                ),
            )
        )
    datasets = list(
        db.execute(
            select(
                StructuredDataset.id,
                StructuredDataset.dataset_key,
                StructuredDataset.name,
                func.count(StructuredObservation.id),
            )
            .join(
                StructuredObservation,
                StructuredObservation.dataset_id == StructuredDataset.id,
            )
            .where(StructuredObservation.country_iso3 == normalized)
            .group_by(StructuredDataset.id)
            .order_by(StructuredDataset.id)
        )
    )
    structured_updated_at = db.scalar(
        select(func.max(StructuredSnapshot.retrieved_at))
        .join(
            StructuredSnapshotObservation,
            StructuredSnapshotObservation.snapshot_id == StructuredSnapshot.id,
        )
        .join(
            StructuredObservation,
            StructuredObservation.id == StructuredSnapshotObservation.observation_id,
        )
        .where(StructuredObservation.country_iso3 == normalized)
    )
    events: list[ResearchEvent] = []
    materials: list[tuple[DocumentVersion, Document, Source]] = []
    if country is not None:
        events = list(
            db.scalars(
                select(ResearchEvent)
                .where(
                    ResearchEvent.country_entity_id == country.id,
                    ResearchEvent.review_status == "reviewed",
                )
                .order_by(ResearchEvent.start_at.desc(), ResearchEvent.id.desc())
                .limit(50)
            )
        )
        materials = list(
            db.execute(
                select(DocumentVersion, Document, Source)
                .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    DocumentEntity.entity_id == country.id,
                    DocumentEntity.review_status == "confirmed",
                )
                .order_by(Document.published_at.desc(), DocumentVersion.id.desc())
                .limit(24)
            )
        )
    key_indicators, indicator_updates = _country_key_indicators(db, normalized)
    policy_rows = _country_policy_documents(db, normalized)
    indicator_updated_at = _latest_datetime(indicator_updates)
    policy_updated_at = _latest_datetime([document.last_seen_at for _, document, _ in policy_rows])
    event_updated_at = _latest_datetime([event.updated_at for event in events])
    updated_at = _latest_datetime(
        [
            *indicator_updates,
            *(document.last_seen_at for _, document, _ in policy_rows),
            structured_updated_at,
            event_updated_at,
        ]
    )
    display = _COUNTRY_DISPLAY.get(normalized, {})
    canonical_name = country.canonical_name if country else catalog_item["name_zh"]
    has_indicators = any(item.get("value") is not None for item in key_indicators)
    has_trends = any(
        len([point for point in item.get("series", []) if point.get("value") is not None]) >= 2
        for item in key_indicators
    )
    has_trade = any(dataset[1] == "un-comtrade-goods-annual-hs" for dataset in datasets)
    map_assets = _verified_map_assets(normalized)
    basemap_asset = _verified_basemap_asset(normalized)
    if basemap_asset is not None:
        basemap_asset.pop("_path", None)
    map_ready = any(item["level"] == "ADM1" for item in map_assets)
    module_status = {
        "overview": "available",
        "administrative-map": "available" if map_ready else "empty",
        "macro": "available" if has_indicators else "empty",
        "trends": "available" if has_trends else "empty",
        "minerals": "empty",
        "trade": "partial" if has_trade else "empty",
        "policy": "available" if policy_rows else "empty",
        "events": "available" if events else "empty",
        "sources": "available",
        "assistant": "available",
    }
    module_freshness = {
        "overview": updated_at,
        "administrative-map": None,
        "macro": indicator_updated_at,
        "trends": indicator_updated_at,
        "minerals": None,
        "trade": structured_updated_at,
        "policy": policy_updated_at,
        "events": event_updated_at,
        "sources": updated_at,
        "assistant": updated_at,
    }
    result = {
        "availability": availability,
        "coverage_level": "deep_sample" if normalized == "COD" else "basic_profile",
        "template_preview_ready": country_profile["preview_enabled"],
        "profile_layer_ready": True,
        "iso3": normalized,
        "iso2": catalog_item["iso2"],
        "m49": catalog_item["m49"],
        "membership": catalog_item["membership"],
        "name": display.get("name_zh") or canonical_name,
        "name_en": display.get("name_en") or catalog_item["name_en"],
        "region": catalog_item["subregion_zh"],
        "region_zh": catalog_item["region_zh"],
        "subregion_zh": catalog_item["subregion_zh"],
        "qa_presets": country_profile["qa_presets"],
        "country_template": country_profile,
        "basic_facts": basic_fact_profile["values"],
        "basic_fact_details": basic_fact_profile["details"],
        "basic_facts_source": basic_fact_profile["source"],
        "updated_at": updated_at,
        "evidence_layer_ready": bool(observations or policy_rows or events),
        "coverage_quality": profile_completeness["quality"],
        "profile_completeness": {
            "filled": profile_completeness["filled"],
            "total": profile_completeness["total"],
            "percent": profile_completeness["percent"],
            "dimensions": profile_completeness["dimensions"],
            "method_note": "完整度只表示登记槽位覆盖，不是国家质量或可信度评分。",
        },
        "map": {
            "current_level": "ADM1" if map_ready else None,
            "available_levels": [item["level"] for item in map_assets],
            "levels": map_assets,
            "future_levels": [
                level
                for level in ("ADM1", "ADM2", "ADM3")
                if level not in {item["level"] for item in map_assets}
            ],
            "future_levels_status": (
                None if len(map_assets) >= 3 else "requires_source_license_and_boundary_review"
            ),
            "cross_level_nesting": "not_inferred_across_source_years",
            "display_scope": "internal_research_preview",
            "basemap": basemap_asset,
        },
        "summary": {
            "events": len(events),
            "materials": coverage_maps["material_counts"].get(normalized, 0),
            "structured_observations": observations or 0,
            "structured_datasets": len(datasets),
        },
        "key_indicators": key_indicators,
        "modules": [
            {
                **module,
                "status": module_status[key],
                "freshness": module_freshness[key],
            }
            for module in country_profile["modules"]
            for key in (module["key"],)
        ],
        "policy_items": [
            _document_summary(version, document, source) for version, document, source in policy_rows
        ],
        "events": [
            {
                **_event_summary(event),
                "review_status": event.review_status,
                "source_count": _event_source_count(db, event.id),
            }
            for event in events
        ],
        "materials": [
            _document_summary(version, document, source) for version, document, source in materials
        ],
        "datasets": [
            {"id": dataset_id, "dataset_key": key, "name": name, "observations": count}
            for dataset_id, key, name, count in datasets
        ],
        "recent_changes": _country_recent_changes(
            events,
            materials,
            datasets,
            structured_updated_at,
        ),
        "source_mix": [
            {
                "source_type": source_type or "unknown",
                "material_count": sum(
                    count
                    for (document_type, item_source_type), count in coverage_maps["material_breakdown"]
                    .get(normalized, {})
                    .items()
                    if item_source_type == source_type
                ),
            }
            for source_type in sorted(
                {source_type for _, source_type in coverage_maps["material_breakdown"].get(normalized, {})},
                key=lambda value: value or "",
            )
        ],
        "source_profiles": [
            {"source_id": source.id, "source_name": source.name, **_source_profile(source)}
            for source in db.scalars(
                select(Source)
                .join(Document, Document.source_id == Source.id)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
                .where(
                    DocumentEntity.entity_id == (country.id if country else -1),
                    DocumentEntity.review_status == "confirmed",
                )
                .distinct()
            )
        ],
        "conflict_summary": _country_conflict_summary(db, country.id if country else None),
    }
    return result


@router.get("/countries/{iso3}/data-catalog")
def get_country_data_catalog(iso3: str, db: DbSession) -> dict[str, Any]:
    normalized = iso3.upper()
    _require_catalog_country(normalized)
    registry = _load_optional_registry(INDICATOR_CATALOG_PATH, key="indicators")
    return country_data_catalog(db, normalized, registry)


@router.get("/countries/{iso3}/readiness")
def get_country_readiness(iso3: str, db: DbSession) -> dict[str, Any]:
    normalized = iso3.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise HTTPException(status_code=422, detail="iso3 must have three letters")
    country = db.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == normalized,
        )
    )
    events: list[ResearchEvent] = []
    recent_updates = 0
    if country is not None:
        events = list(
            db.scalars(
                select(ResearchEvent).where(
                    ResearchEvent.country_entity_id == country.id,
                    ResearchEvent.review_status == "reviewed",
                )
            )
        )
        recent_updates = (
            db.scalar(
                select(func.count(func.distinct(DocumentVersion.id)))
                .select_from(DocumentEntity)
                .join(DocumentVersion, DocumentVersion.id == DocumentEntity.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentEntity.entity_id == country.id,
                    DocumentEntity.review_status == "confirmed",
                    func.coalesce(DocumentVersion.published_at, Document.published_at)
                    >= datetime.now(UTC) - timedelta(days=90),
                )
            )
            or 0
        )
    three_source_events = sum(_event_source_count(db, event.id) >= 3 for event in events)
    harvest = _drc_harvest_summary() if normalized == "COD" else {}
    candidate_sources = int(harvest.get("source_count", 0))
    verified_sources = int(harvest.get("verified_source_count", 0))
    candidate_items = int(harvest.get("item_count", 0))
    pending_files = int(harvest.get("data_team_request_count", 0))
    resolved_files = int(harvest.get("resolved_data_team_request_count", 0))
    active_configs = (
        db.scalar(
            select(func.count()).select_from(CapabilityConfig).where(CapabilityConfig.status == "active")
        )
        or 0
    )
    successful_runs = (
        db.scalar(
            select(func.count(func.distinct(CapabilityRun.config_id)))
            .select_from(CapabilityRun)
            .where(CapabilityRun.status == "succeeded")
        )
        or 0
    )
    capabilities_seeded = active_configs >= 3 and successful_runs >= 3
    agent_claims_valid = _stored_agent_claims_valid(db, normalized)
    map_asset_manifest_valid = _map_asset_manifest_valid(normalized)
    static_fact_check_passed = _static_fact_check_passed()
    gaps: list[str] = []
    if three_source_events < 5:
        gaps.append(f"仍需补齐 {5 - three_source_events} 个达到三源门槛的原子事件。")
    if recent_updates < 12:
        gaps.append(f"仍需人工确认 {12 - recent_updates} 条近 90 天动态。")
    if pending_files:
        gaps.append(f"仍有 {pending_files} 份 P0 官方原文件待数据组交付。")
    if verified_sources < candidate_sources:
        gaps.append(f"{candidate_sources - verified_sources} 个候选来源仍处于安全复核状态。")
    if not capabilities_seeded:
        gaps.append("三个能力尚未全部建立配置并各完成一次真实运行。")
    if not agent_claims_valid:
        gaps.append("尚无通过事实—证据映射校验的 Agent 运行。")
    if not map_asset_manifest_valid:
        gaps.append("全球标准地图或行政区地图资产清单尚未通过来源、许可与哈希校验。")
    if not static_fact_check_passed:
        gaps.append("Reader 静态资源中仍包含禁止的演示事实片段。")
    return {
        "iso3": normalized,
        "status": "ready" if not gaps else "partial",
        "generated_at": harvest.get("generated_at"),
        "metrics": {
            "candidate_sources": candidate_sources,
            "verified_candidate_sources": verified_sources,
            "candidate_metadata_items": candidate_items,
            "reviewed_events": len(events),
            "three_source_events": three_source_events,
            "recent_confirmed_updates": recent_updates,
            "pending_official_files": pending_files,
            "resolved_official_file_requests": resolved_files,
            "capabilities_seeded": capabilities_seeded,
            "agent_claims_valid": agent_claims_valid,
            "static_fact_check_passed": static_fact_check_passed,
            "map_asset_manifest_valid": map_asset_manifest_valid,
        },
        "targets": {
            "three_source_events": 5,
            "recent_confirmed_updates": 12,
            "pending_official_files": 0,
            "capability_configs_with_successful_runs": 3,
        },
        "gaps": gaps,
        "notes": harvest.get("official_evidence_notes", []),
        "policy": "候选元数据不自动成为事件事实；只有人工确认的实体关联和多源证据可供 Agent 引用。",
    }


def _drc_harvest_summary() -> dict[str, Any]:
    try:
        payload = json.loads(DRC_MVP_HARVEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        return {}
    requests = [request for request in payload.get("data_team_requests", []) if isinstance(request, dict)]
    closed_requests = [request for request in requests if _official_request_closed(request)]
    notes = [
        note
        for request in closed_requests
        for note in [request.get("archive_follow_up")]
        if isinstance(note, str) and note
    ]
    return {
        "generated_at": payload.get("generated_at"),
        **summary,
        "data_team_request_count": len(requests) - len(closed_requests),
        "resolved_data_team_request_count": len(closed_requests),
        "official_evidence_notes": notes,
    }


def _drc_verified_harvest_reports(
    *,
    country_iso3: str | None,
    q: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[dict[str, Any]]:
    try:
        payload = json.loads(DRC_MVP_HARVEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    iso3 = str(payload.get("country_code") or "").strip().upper()
    if iso3 != "COD" or (country_iso3 and country_iso3 != iso3):
        return []
    country_name = str(payload.get("country_name") or iso3)
    query = str(q or "").strip().casefold()
    generated_at = _report_datetime(payload.get("generated_at"))
    reports_by_url: dict[str, dict[str, Any]] = {}
    for source in payload.get("sources", []):
        if not isinstance(source, dict) or source.get("status") != "metadata_verified":
            continue
        if source.get("repeat_consistent") is False:
            continue
        source_name = str(source.get("name") or source.get("organization") or "已登记信源")
        observed_at = max(
            (
                value
                for fetch in source.get("fetches", [])
                if isinstance(fetch, dict)
                for value in [_report_datetime(fetch.get("fetched_at"))]
                if value is not None
            ),
            default=generated_at,
        )
        for item in source.get("items", []):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            canonical_url = str(item.get("url") or item.get("discovery_url") or "").strip()
            if not title or not canonical_url:
                continue
            if query and query not in f"{title} {source_name}".casefold():
                continue
            published_at = _report_datetime(item.get("published_at"))
            if date_from and (
                published_at is None or _report_sort_value(published_at) < _report_sort_value(date_from)
            ):
                continue
            if date_to and (
                published_at is None or _report_sort_value(published_at) > _report_sort_value(date_to)
            ):
                continue
            reports_by_url.setdefault(
                canonical_url,
                {
                    "document_id": None,
                    "document_version_id": None,
                    "version_no": None,
                    "title": title,
                    "published_at": published_at,
                    "published_at_precision": item.get("published_at_precision") or "unknown",
                    "observed_at": observed_at,
                    "recorded_at": _report_datetime(item.get("retrieved_at")) or observed_at,
                    "updated_at": _report_datetime(item.get("retrieved_at")) or observed_at,
                    "language": item.get("language") or "und",
                    "document_type": (
                        "report"
                        if str(item.get("media_type") or "").lower() == "application/pdf"
                        else "article"
                    ),
                    "source_id": None,
                    "source_key": source.get("source_id"),
                    "source_name": source_name,
                    "source_type": _harvest_source_type(source.get("category")),
                    "canonical_url": canonical_url,
                    "discovery_url": item.get("discovery_url") or canonical_url,
                    "source_url": canonical_url,
                    "abstract": None,
                    "metadata": {},
                    "review_status": "source_verified",
                    "feed_status": "reported",
                    "aggregation_status": "pending_platform_aggregation",
                    "countries": [{"iso3": iso3, "name": country_name}],
                    "linked_events": [],
                    "actions": {
                        "can_open_source": True,
                        "can_add_to_project": False,
                        "requires_researcher_review": False,
                    },
                },
            )
    return list(reports_by_url.values())


def _report_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _report_sort_value(value: Any) -> float:
    parsed = _report_datetime(value)
    return parsed.timestamp() if parsed else float("-inf")


def _harvest_source_type(category: Any) -> str:
    label = str(category or "")
    if "媒体" in label:
        return "news_media"
    if "学术" in label or "期刊" in label:
        return "academic_journal"
    if "数据" in label:
        return "official_data"
    if "研究" in label:
        return "research_organization"
    return "government_or_international"


def _official_request_closed(request: dict[str, Any]) -> bool:
    if request.get("status") not in {"closed", "closed_for_mvp"}:
        return False
    if request.get("status") == "closed_for_mvp" and not request.get("archive_follow_up"):
        return False
    evidence = request.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    for item in evidence:
        if not isinstance(item, dict):
            return False
        sha256 = item.get("sha256")
        if (
            not str(item.get("url", "")).startswith("https://")
            or not isinstance(item.get("content_type"), str)
            or not isinstance(item.get("size_bytes"), int)
            or item["size_bytes"] <= 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            return False
        try:
            int(sha256, 16)
        except ValueError:
            return False
    return True


def _map_asset_manifest_valid(iso3: str) -> bool:
    if not _adm1_map_asset_valid(iso3):
        return False
    try:
        manifest = json.loads(MAP_ASSET_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    global_map = manifest.get("global_map", {})
    interactive_map = manifest.get("interactive_global_map", {})
    map_runtime = manifest.get("map_runtime", {})
    project_root = Path(__file__).resolve().parents[3]
    try:
        standard_path = project_root / global_map["path"]
        interactive_path = project_root / interactive_map["path"]
        runtime_css_path = project_root / map_runtime["css_path"]
        runtime_js_path = project_root / map_runtime["js_path"]
        standard_digest = hashlib.sha256(standard_path.read_bytes()).hexdigest()
        interactive_digest = hashlib.sha256(interactive_path.read_bytes()).hexdigest()
        runtime_css_digest = hashlib.sha256(runtime_css_path.read_bytes()).hexdigest()
        runtime_js_digest = hashlib.sha256(runtime_js_path.read_bytes()).hexdigest()
    except (KeyError, OSError):
        return False
    return all(
        (
            global_map.get("status") == "verified_standard_map_direct_use",
            standard_digest == global_map.get("sha256"),
            interactive_map.get("status") == "internal_demo_only",
            interactive_digest == interactive_map.get("sha256"),
            interactive_map.get("country_count") == 195,
            bool(interactive_map.get("license")),
            runtime_css_digest == map_runtime.get("css_sha256"),
            runtime_js_digest == map_runtime.get("js_sha256"),
            bool(map_runtime.get("license")),
        )
    )


def _adm1_map_asset_valid(iso3: str) -> bool:
    return any(item["level"] == "ADM1" for item in _verified_map_assets(iso3))


def _verified_map_assets(iso3: str) -> list[dict[str, Any]]:
    try:
        manifest = json.loads(MAP_ASSET_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    assets: list[dict[str, Any]] = []
    for item in manifest.get("assets", []):
        if item.get("iso3") != iso3 or item.get("status") != "verified":
            continue
        path = Path(__file__).resolve().parents[1] / "static" / "reader" / item.get("path", "")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest != item.get("sha256") or not item.get("license"):
            continue
        assets.append(
            {
                "level": item.get("level"),
                "asset_path": item.get("path"),
                "boundary_id": item.get("boundary_id"),
                "canonical_name": item.get("canonical_name"),
                "represented_year": item.get("represented_year"),
                "unit_count": item.get("unit_count"),
                "source_url": item.get("source_url"),
                "license": item.get("license"),
                "attribution": item.get("attribution"),
                "limitations": item.get("limitations"),
                "public_use_rule": item.get("public_use_rule"),
            }
        )
    return sorted(assets, key=lambda item: int(str(item["level"]).removeprefix("ADM") or 0))


def _verified_basemap_asset(iso3: str, asset_id: str | None = None) -> dict[str, Any] | None:
    try:
        manifest = json.loads(MAP_ASSET_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    static_root = Path(__file__).resolve().parents[1] / "static" / "reader"
    for item in manifest.get("basemaps", []):
        if item.get("iso3") != iso3 or item.get("status") != "verified":
            continue
        if asset_id is not None and item.get("id") != asset_id:
            continue
        path = static_root / str(item.get("path") or "")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        if digest != item.get("sha256") or not item.get("license"):
            continue
        return {
            "asset_id": item["id"],
            "url": f"/api/v1/reader/map-assets/{item['id']}.pmtiles",
            "format": item.get("format"),
            "bounds": item.get("bounds"),
            "min_zoom": item.get("min_zoom"),
            "max_zoom": item.get("max_zoom"),
            "snapshot_date": item.get("snapshot_date"),
            "sha256": digest,
            "license": item.get("license"),
            "license_url": item.get("license_url"),
            "attribution": item.get("attribution"),
            "content_layers": item.get("content_layers", []),
            "public_use_rule": item.get("public_use_rule"),
            "_path": path,
        }
    return None


@router.get("/map-assets/{asset_id}.pmtiles")
def get_map_asset(asset_id: str, request: Request) -> Response:
    asset = _verified_basemap_asset("COD", asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="map asset not found")
    path = Path(asset.pop("_path"))
    size = path.stat().st_size
    etag = f'"{asset["sha256"]}"'
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=86400, immutable",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type="application/vnd.pmtiles", headers=headers)
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    else:
        length = min(int(match.group(2)), size)
        start, end = size - length, size - 1
    if start >= size or end < start:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    end = min(end, size - 1)
    with path.open("rb") as handle:
        handle.seek(start)
        content = handle.read(end - start + 1)
    return Response(
        content=content,
        status_code=206,
        media_type="application/vnd.pmtiles",
        headers={
            **headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(len(content)),
        },
    )


def _stored_agent_claims_valid(db: Session, iso3: str) -> bool:
    runs = list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.country_iso3 == iso3, AgentRun.status == "succeeded")
            .order_by(AgentRun.id.desc())
            .limit(20)
        )
    )
    for run in runs:
        evidence_ids = {item.get("evidence_id") for item in run.evidence if isinstance(item, dict)}
        claims = [
            claim
            for section in run.artifact.get("sections", [])
            if isinstance(section, dict)
            for claim in section.get("claims", [])
            if isinstance(claim, dict)
        ]
        if claims and all(
            claim.get("fact_ids")
            and claim.get("evidence_ids")
            and not (set(claim["evidence_ids"]) - evidence_ids)
            for claim in claims
        ):
            return True
    return False


def _static_fact_check_passed() -> bool:
    static_root = Path(__file__).resolve().parents[1] / "static" / "reader"
    forbidden = ("全球70%+", "7500万吨", "50%暴利税", "M23 武装与政府军", "正在运行 · 日常追踪")
    try:
        contents = "\n".join(
            (static_root / name).read_text(encoding="utf-8")
            for name in ("index.html", "app.js", "country_space_data.js")
        )
    except OSError:
        return False
    return not any(fragment in contents for fragment in forbidden)


class CountryQuestionPayload(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    output_type: str = Field(default="country_analysis", min_length=1, max_length=64)


class AssistantContextPayload(BaseModel):
    space: str
    country_iso3: str | None = Field(default="COD", pattern=r"^[A-Z]{3}$")
    event_id: int | None = Field(default=None, gt=0)
    research_case_id: int | None = Field(default=None, gt=0)
    capability_config_id: int | None = Field(default=None, gt=0)
    capability_config_ids: list[int] = Field(default_factory=list, max_length=3)
    capability_run_id: int | None = Field(default=None, gt=0)
    focus_type: str | None = Field(default=None, min_length=1, max_length=48)
    focus_key: str | None = Field(default=None, min_length=1, max_length=160)
    focus_label: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_scope(self) -> AssistantContextPayload:
        normalized = {
            "countries": "country",
            "events": "event",
            "topics": "topic",
            "project": "topic",
            "projects": "topic",
            "capabilities": "capability",
            "resources": "resource",
            "system-status": "system",
        }.get(self.space, self.space)
        if normalized not in {"country", "event", "topic", "capability", "resource", "system"}:
            raise ValueError("unsupported assistant space")
        self.space = normalized
        selected_ids = list(dict.fromkeys(self.capability_config_ids))
        if self.capability_config_id and self.capability_config_id not in selected_ids:
            selected_ids.insert(0, self.capability_config_id)
        if len(selected_ids) > 3:
            raise ValueError("at most 3 capability configurations can be selected")
        self.capability_config_ids = selected_ids
        self.capability_config_id = selected_ids[0] if selected_ids else None
        return self


class CountryReferencePayload(BaseModel):
    type: str = Field(pattern=r"^(document_version|event|observation)$")
    id: int = Field(gt=0)
    snapshot_id: int | None = Field(default=None, gt=0)
    field: str | None = Field(default=None, max_length=80)
    quote: str | None = Field(default=None, min_length=1, max_length=1500)
    start: int | None = Field(default=None, ge=0)


class AssistantQuestionPayload(BaseModel):
    references: list[CountryReferencePayload] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_references(self):
        if sum(len(ref.quote or "") for ref in self.references) > 6000:
            raise ValueError("selected excerpts exceed 6000 characters")
        return self

    conversation_id: str | None = Field(default=None, min_length=8, max_length=36)
    context: AssistantContextPayload
    question: str = Field(min_length=1, max_length=500)
    online_mode: str = Field(default="auto", pattern=r"^(off|auto|on)$")
    answer_mode: str = Field(default="research_chat", pattern=r"^(research_chat|formal_artifact)$")


def _assistant_case_id(scope_type: str, scope_key: str, scope_context: dict[str, Any]) -> int | None:
    if scope_type != "topic" or not scope_key.isdigit():
        return None
    key_value = int(scope_key)
    raw_value = scope_context.get("research_case_id", key_value)
    try:
        return key_value if int(raw_value) == key_value else None
    except (TypeError, ValueError):
        return None


def _require_assistant_case_access(
    db: Session,
    case_id: int | None,
    raw_key: str | None,
    permission: str = "view",
) -> User:
    actor = _current_reader_actor(db, raw_key)
    if actor.id is None:
        db.flush()
    if case_id is None or not _reader_identity_initialized(db):
        return actor
    try:
        require_case_permission(db, case_id, actor, permission)
    except ReaderAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    db.commit()
    return actor


def _require_assistant_run_access(
    db: Session,
    run: AgentRun,
    raw_key: str | None,
    permission: str = "view",
) -> User:
    case_id = _assistant_case_id(run.scope_type, run.scope_key, run.scope_context or {})
    if run.scope_type == "topic" and run.scope_key != "overview" and case_id is None:
        raise HTTPException(status_code=404, detail="assistant run not found")
    if case_id is not None:
        return _require_assistant_case_access(db, case_id, raw_key, permission)
    keys_initialized = _reader_identity_initialized(db)
    actor = _current_reader_actor(db, raw_key)
    if actor.id is None:
        db.flush()
    allowed_owner_ids = {actor.id} if keys_initialized else {None, actor.id}
    if run.requested_by not in allowed_owner_ids:
        raise HTTPException(status_code=404, detail="assistant run not found")
    return actor


def _validate_assistant_capability_selection(
    db: Session,
    context: AssistantContextPayload,
    raw_key: str | None,
) -> None:
    if not context.capability_config_ids:
        return
    actor = _current_reader_actor(db, raw_key)
    for config_id in context.capability_config_ids:
        config = db.get(CapabilityConfig, config_id)
        template = db.get(CapabilityTemplate, config.template_id) if config else None
        permission = "view" if context.space == "capability" else "run_skill"
        if config is None or not _capability_config_visible(db, config, actor, permission):
            raise HTTPException(
                status_code=403,
                detail="selected capability is outside the current user's scope",
            )
        if context.space == "capability":
            continue
        if config.research_case_id and config.research_case_id != context.research_case_id:
            raise HTTPException(
                status_code=403,
                detail="selected capability is outside the current research scope",
            )
        supported_scopes = set(_skill_manifest_v2(template).get("supported_scopes") or [])
        accepted_scopes = {context.space, "research_case"} if context.space == "topic" else {context.space}
        prerequisites, prerequisites_ready = _capability_prerequisites(template, config, db)
        if accepted_scopes.isdisjoint(supported_scopes):
            raise HTTPException(
                status_code=422,
                detail="selected capability does not support this research space",
            )
        if (
            config.status not in {"active", "published"}
            or template.status != "active"
            or template.validation_status != "verified"
            or not prerequisites_ready
        ):
            missing = "、".join(prerequisites)
            detail = "selected capability is not runnable"
            raise HTTPException(status_code=409, detail=f"{detail}: {missing}" if missing else detail)
        readiness = _capability_runtime_readiness(template, config, db)
        if not readiness["verified_usable"]:
            raise HTTPException(
                status_code=409,
                detail="selected capability has not passed a successful run for its current configuration",
            )


def _format_indicator_fact_value(value: Any) -> str:
    number = Decimal(str(value))
    if number == number.to_integral_value():
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def _format_indicator_unit(unit: str | None) -> str:
    return {
        "person": "人",
        "USD": "美元",
        "percent": "%",
        "percent_of_GDP": "%（占 GDP）",
        "million_USD": "百万美元",
    }.get(unit or "", unit or "")


@router.post("/countries/{iso3}/ask")
def ask_country_assistant(
    iso3: str,
    payload: CountryQuestionPayload,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    normalized = iso3.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise HTTPException(status_code=422, detail="iso3 must have three letters")
    actor = _current_reader_actor(db, x_reader_key)
    if actor.id is None:
        db.flush()
    response = _run_assistant_request(
        db,
        AssistantQuestionPayload(
            context=AssistantContextPayload(space="country", country_iso3=normalized),
            question=payload.question,
            online_mode="off",
        ),
        output_type=payload.output_type,
        requested_by=actor.id,
    )
    response["iso3"] = normalized
    response["country_name"] = _country_name(normalized)
    return response


@router.post("/assistant/ask")
def ask_research_assistant(
    payload: AssistantQuestionPayload,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    actor = _current_reader_actor(db, x_reader_key)
    if actor.id is None:
        db.flush()
    _require_assistant_case_access(db, payload.context.research_case_id, x_reader_key)
    _validate_assistant_capability_selection(db, payload.context, x_reader_key)
    return _run_assistant_request(db, payload, requested_by=actor.id)


@router.post("/assistant/stream")
def stream_research_assistant(
    payload: AssistantQuestionPayload,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> StreamingResponse:
    actor = _current_reader_actor(db, x_reader_key)
    if actor.id is None:
        db.flush()
    _require_assistant_case_access(db, payload.context.research_case_id, x_reader_key)
    _validate_assistant_capability_selection(db, payload.context, x_reader_key)
    agent_run = _create_assistant_run(db, payload, requested_by=actor.id)
    worker_sessions = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    return StreamingResponse(
        _assistant_event_stream(
            worker_sessions,
            payload=payload,
            run_id=agent_run.id,
            conversation_id=agent_run.conversation_id or "",
            scope_type=agent_run.scope_type,
            scope_key=agent_run.scope_key,
            runtime=agent_run.runtime,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/assistant/runs/{run_id}")
def get_assistant_stream_run(
    run_id: int,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    agent_run = db.get(AgentRun, run_id)
    if agent_run is None:
        raise HTTPException(status_code=404, detail="assistant run not found")
    _require_assistant_run_access(db, agent_run, x_reader_key)
    return _assistant_run_status(agent_run)


@router.post("/assistant/runs/{run_id}/cancel")
def cancel_assistant_stream_run(
    run_id: int,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    require_reader_write_access()
    agent_run = db.get(AgentRun, run_id)
    if agent_run is None:
        raise HTTPException(status_code=404, detail="assistant run not found")
    _require_assistant_run_access(db, agent_run, x_reader_key, "edit")
    if agent_run.status != "running":
        return {"run_id": run_id, "status": agent_run.status, "cancel_requested": False}
    with _ASSISTANT_RUN_PROGRESS_LOCK:
        _ASSISTANT_CANCEL_REQUESTS.add(run_id)
    _fail_assistant_run(db, agent_run, "user_canceled", "用户已停止本轮研究。")
    return {"run_id": run_id, "status": "failed", "cancel_requested": True}


@router.get("/countries/{iso3}/assistant-conversations")
def list_country_assistant_conversations(
    iso3: str,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    normalized = iso3.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise HTTPException(status_code=422, detail="iso3 must have three letters")
    keys_initialized = _reader_identity_initialized(db)
    actor = _current_reader_actor(db, x_reader_key)
    if actor.id is None:
        db.flush()
    owner_filter = AgentRun.requested_by == actor.id
    if not keys_initialized:
        owner_filter = or_(owner_filter, AgentRun.requested_by.is_(None))
    runs = list(
        db.scalars(
            select(AgentRun)
            .where(
                AgentRun.scope_type == "country",
                AgentRun.scope_key == normalized,
                AgentRun.conversation_id.is_not(None),
                owner_filter,
            )
            .order_by(AgentRun.updated_at.desc(), AgentRun.id.desc())
        )
    )
    grouped: dict[str, list[AgentRun]] = defaultdict(list)
    for run in runs:
        if run.conversation_id:
            grouped[run.conversation_id].append(run)
    items = []
    grouped_items = list(grouped.items())
    for conversation_id, conversation_runs in grouped_items[offset : offset + limit]:
        latest = conversation_runs[0]
        first = conversation_runs[-1]
        items.append(
            {
                "conversation_id": conversation_id,
                "title": first.question,
                "status": latest.status,
                "turn_count": len(conversation_runs),
                "latest_run_id": latest.id,
                "created_at": first.created_at,
                "updated_at": latest.updated_at,
            }
        )
    total = len(grouped_items)
    return {
        "country_iso3": normalized,
        "items": items,
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }


@router.get("/assistant/conversations/{conversation_id}")
def get_assistant_conversation(
    conversation_id: str,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    candidates = list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.created_at, AgentRun.id)
        )
    )
    if not candidates:
        raise HTTPException(status_code=404, detail="assistant conversation not found")
    anchor: AgentRun | None = None
    access_error: HTTPException | None = None
    for candidate in candidates:
        try:
            _require_assistant_run_access(db, candidate, x_reader_key)
        except HTTPException as exc:
            if exc.status_code == 401:
                raise
            access_error = exc
            continue
        anchor = candidate
        break
    if anchor is None:
        if access_error is not None and access_error.status_code == 403:
            raise access_error
        raise HTTPException(status_code=404, detail="assistant conversation not found")
    runs = []
    for candidate in candidates:
        if (candidate.scope_type, candidate.scope_key) != (anchor.scope_type, anchor.scope_key):
            continue
        try:
            _require_assistant_run_access(db, candidate, x_reader_key)
        except HTTPException:
            continue
        runs.append(candidate)
    return {
        "conversation_id": conversation_id,
        "scope_type": anchor.scope_type,
        "scope_key": anchor.scope_key,
        "scope_context": anchor.scope_context,
        "turns": [
            {
                "run_id": run.id,
                "question": run.question,
                "references": (run.scope_context or {}).get("references", []),
                "status": run.status,
                "answer": artifact_markdown_from_dict(run.artifact),
                "artifact": run.artifact,
                "plan": run.plan,
                "tool_trace": run.tool_trace,
                "runtime": run.runtime,
                "error": (
                    {"code": run.error_code, "message": run.error_message} if run.status == "failed" else None
                ),
                "created_at": run.created_at,
            }
            for run in runs
        ],
    }


def _country_key_indicators(
    db: Session,
    iso3: str,
) -> tuple[list[dict[str, Any]], list[datetime]]:
    cards: list[dict[str, Any]] = []
    updates: list[datetime] = []
    specs = _COUNTRY_INDICATORS.get(iso3, ())
    if not specs:
        return cards, updates

    dataset_keys = {spec["dataset_key"] for spec in specs}
    datasets = list(
        db.scalars(select(StructuredDataset).where(StructuredDataset.dataset_key.in_(dataset_keys)))
    )
    dataset_map = {d.dataset_key: d for d in datasets}

    snapshot_map: dict[int, StructuredSnapshot] = {}
    for d in datasets:
        snap = db.scalar(
            select(StructuredSnapshot)
            .where(StructuredSnapshot.dataset_id == d.id)
            .order_by(StructuredSnapshot.retrieved_at.desc(), StructuredSnapshot.id.desc())
            .limit(1)
        )
        if snap:
            snapshot_map[d.id] = snap

    for spec in specs:
        dataset = dataset_map.get(spec["dataset_key"])
        snapshot = snapshot_map.get(dataset.id) if dataset else None
        rows: list[tuple[StructuredObservation, StructuredObservationVersion]] = []
        if snapshot is not None and dataset is not None:
            updates.append(snapshot.retrieved_at)
            rows = list(
                db.execute(
                    select(StructuredObservation, StructuredObservationVersion)
                    .join(
                        StructuredSnapshotObservation,
                        StructuredSnapshotObservation.observation_id == StructuredObservation.id,
                    )
                    .join(
                        StructuredObservationVersion,
                        and_(
                            StructuredObservationVersion.id
                            == StructuredSnapshotObservation.observation_version_id,
                            StructuredObservationVersion.observation_id
                            == StructuredSnapshotObservation.observation_id,
                        ),
                    )
                    .where(
                        StructuredSnapshotObservation.snapshot_id == snapshot.id,
                        StructuredObservation.dataset_id == dataset.id,
                        StructuredObservation.country_iso3 == iso3,
                        StructuredObservation.indicator_code == spec["indicator_code"],
                        StructuredObservation.metric_code == spec["metric_code"],
                    )
                    .order_by(StructuredObservation.period)
                )
            )
        latest = next((row for row in reversed(rows) if row[1].value is not None), None)
        latest_observation = latest[0] if latest else None
        latest_version = latest[1] if latest else None
        most_recent_version = rows[-1][1] if rows else None
        numeric_values = [float(version.value) for _, version in rows if version.value is not None]
        axis_min = min(numeric_values) if numeric_values else None
        axis_max = max(numeric_values) if numeric_values else None
        if axis_min is not None and axis_max is not None:
            spread = axis_max - axis_min
            padding = spread * 0.08 if spread else max(abs(axis_min) * 0.05, 1.0)
            display_min = axis_min - padding
            display_max = axis_max + padding
        else:
            display_min = None
            display_max = None
        series_analysis = _indicator_series_analysis(rows)
        cards.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "dataset_id": dataset.id if dataset else None,
                "dataset_key": spec["dataset_key"],
                "dataset_name": dataset.name if dataset else None,
                "source_name": spec["source_name"],
                "updated_at": snapshot.retrieved_at if snapshot else None,
                "period": latest_observation.period if latest_observation else None,
                "latest_period": latest_observation.period if latest_observation else None,
                "value": latest_version.value if latest_version else None,
                "unit": latest_version.unit if latest_version else None,
                "axes": {
                    "x": {"label": "年份", "frequency": "annual"},
                    "y": {
                        "label": spec["label"],
                        "unit": latest_version.unit if latest_version else None,
                        "scale": "linear",
                        "domain": [display_min, display_max],
                        "non_zero_origin": bool(display_min is not None and display_min > 0),
                        "domain_rule": "按有效值范围增加 8% 留白；单值增加 5% 留白。",
                    },
                },
                "source_metadata": {
                    "provider": spec["source_name"],
                    "publication_at": (
                        (snapshot.source_metadata or {}).get("publication_date") if snapshot else None
                    ),
                    "retrieved_at": snapshot.retrieved_at if snapshot else None,
                    "actual_latest_period": latest_observation.period if latest_observation else None,
                },
                "source_url": (
                    latest_version.source_url
                    if latest_version
                    else most_recent_version.source_url
                    if most_recent_version
                    else None
                ),
                "missing_reason": (
                    None
                    if latest_version
                    else most_recent_version.missing_reason
                    if most_recent_version
                    else "not_available"
                ),
                "series_analysis": series_analysis,
                "series": [
                    {
                        "period": observation.period,
                        "value": version.value,
                        "unit": version.unit,
                        "missing_reason": version.missing_reason,
                    }
                    for observation, version in rows
                ],
            }
        )
    return cards, updates


def _indicator_series_analysis(
    rows: list[tuple[StructuredObservation, StructuredObservationVersion]],
) -> dict[str, Any]:
    valid = [
        (observation.period, version.value) for observation, version in rows if version.value is not None
    ]
    changes: list[dict[str, Any]] = []
    for (previous_period, previous_value), (period, value) in zip(valid, valid[1:], strict=False):
        period_gap = period - previous_period
        absolute_change = value - previous_value
        change_rate = absolute_change / abs(previous_value) * Decimal("100") if previous_value != 0 else None
        changes.append(
            {
                "period": period,
                "previous_period": previous_period,
                "period_gap": period_gap,
                "is_year_over_year": period_gap == 1,
                "absolute_change": absolute_change,
                "change_rate_percent": change_rate,
            }
        )
    first_period, first_value = valid[0] if valid else (None, None)
    latest_period, latest_value = valid[-1] if valid else (None, None)
    period_span = (
        latest_period - first_period if first_period is not None and latest_period is not None else 0
    )
    cagr_percent = None
    if period_span > 0 and first_value is not None and latest_value is not None and first_value > 0:
        cagr_percent = (float(latest_value / first_value) ** (1 / period_span) - 1) * 100
    yearly_rates = [
        float(item["change_rate_percent"])
        for item in changes
        if item["is_year_over_year"] and item["change_rate_percent"] is not None
    ]
    return {
        "series_kind": "discrete_annual_observations",
        "observation_count": len(valid),
        "first_period": first_period,
        "latest_period": latest_period,
        "annual_changes": changes,
        "latest_annual_change": next(
            (item for item in reversed(changes) if item["is_year_over_year"]),
            None,
        ),
        "cagr_percent": cagr_percent,
        "year_over_year_rate_range": ([min(yearly_rates), max(yearly_rates)] if yearly_rates else None),
        "representation_note": (
            "各点是来源返回的离散年度观测值；线段只连接相邻有效观测，不代表线性函数、插值或未来预测。"
        ),
    }


def _country_policy_documents(
    db: Session,
    iso3: str,
) -> list[tuple[DocumentVersion, Document, Source]]:
    country_id = db.scalar(
        select(ResearchEntity.id).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == iso3,
        )
    )
    if country_id is None:
        return []
    rows = list(
        db.execute(
            select(DocumentVersion, Document, Source)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .join(EventMention, EventMention.document_version_id == DocumentVersion.id)
            .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
            .join(
                DocumentEntity,
                DocumentEntity.document_version_id == DocumentVersion.id,
            )
            .where(
                DocumentVersion.version_no == Document.latest_version_no,
                DocumentEntity.entity_id == country_id,
                DocumentEntity.review_status == "confirmed",
                ResearchEvent.country_entity_id == country_id,
                ResearchEvent.event_type == "policy",
                ResearchEvent.review_status == "reviewed",
                EventMention.review_status == "confirmed",
            )
            .order_by(
                func.coalesce(
                    DocumentVersion.published_at,
                    Document.published_at,
                    Document.first_seen_at,
                ).desc(),
                Document.id.desc(),
            )
        )
    )
    if get_settings().public_demo_enabled:
        rows.extend(
            db.execute(
                select(DocumentVersion, Document, Source)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
                .where(
                    DocumentVersion.version_no == Document.latest_version_no,
                    DocumentEntity.entity_id == country_id,
                    DocumentEntity.review_status == "confirmed",
                    Document.document_type.in_(["policy", "law", "regulation", "policy_document"]),
                )
                .order_by(Document.published_at.desc())
            )
        )
    return list({row[0].id: row for row in rows}.values())


def _country_agent_tools(
    db: Session,
    *,
    iso3: str,
    country_name: str,
    indicators: list[dict[str, Any]],
    policy_rows: list[tuple[DocumentVersion, Document, Source]],
) -> list[RegisteredAgentTool]:
    basic_fact_profile = _country_basic_fact_profile(iso3)
    basic_fact_evidence_id = f"country-basic-facts:{iso3}"
    basic_fact_evidence = EvidenceItem(
        evidence_id=basic_fact_evidence_id,
        kind="country_reference_profile",
        title=f"{country_name}基础国情档案",
        source_name=basic_fact_profile["source"]["name"],
        source_url=basic_fact_profile["source"]["snapshot_url"],
        published_at=basic_fact_profile["source"]["revision_date"],
        observed_at=basic_fact_profile["source"]["retrieved_at"],
        retrieved_at=basic_fact_profile["source"]["retrieved_at"],
        published_at_precision="day",
        evidence_locator={
            "registry": "data/country_basic_facts.json",
            "country_iso3": iso3,
            "revision": basic_fact_profile["source"]["revision"],
        },
        origin="reference_registry",
        verification_status="snapshot_pinned",
        payload=basic_fact_profile["details"],
    )
    basic_fact_labels = {
        "capital": "首都",
        "currency": "货币",
        "official_languages": "官方语言",
        "land_area": "国土面积",
    }
    basic_fact_facts = [
        FactItem(
            fact_id=f"fact:country-basic-facts:{iso3}:{key}",
            text=f"{country_name}的{basic_fact_labels[key]}为{value}。",
            evidence_ids=[basic_fact_evidence_id],
            origin="reference_registry",
            verification_status="snapshot_pinned",
        )
        for key, value in basic_fact_profile["values"].items()
    ]
    basic_fact_lines = [fact.text for fact in basic_fact_facts]
    indicator_evidence: list[EvidenceItem] = []
    indicator_facts: list[FactItem] = []
    trend_facts: list[FactItem] = []
    calculations: list[dict[str, Any]] = []
    indicator_lines: list[str] = []
    for item in indicators:
        if item.get("value") is None:
            continue
        evidence_id = f"indicator:{iso3}:{item['key']}:{item.get('period') or 'latest'}"
        indicator_evidence.append(
            EvidenceItem(
                evidence_id=evidence_id,
                kind="indicator",
                title=f"{item['label']}时序统计",
                source_name=item["source_name"],
                source_url=item.get("source_url"),
                published_at=None,
                observed_at=str(item.get("period")) if item.get("period") else None,
                published_at_precision="year",
                evidence_locator={
                    "dataset_key": item.get("dataset_key"),
                    "period": item.get("period"),
                    "indicator": item.get("key"),
                },
                payload={
                    "label": item["label"],
                    "period": item.get("period"),
                    "value": item.get("value"),
                    "unit": item.get("unit"),
                    "missing_reason": item.get("missing_reason"),
                },
            )
        )
        fact_text = (
            f"{item['label']}为 {_format_indicator_fact_value(item['value'])} "
            f"{_format_indicator_unit(item.get('unit'))}"
            f"（统计期 {item.get('period')}，来源 {item['source_name']}）。"
        )
        indicator_lines.append(fact_text)
        indicator_facts.append(
            FactItem(
                fact_id=f"fact:{evidence_id}",
                text=fact_text,
                evidence_ids=[evidence_id],
            )
        )
        valued_series = [point for point in item.get("series", []) if point.get("value") is not None]
        if len(valued_series) >= 2:
            previous, latest = valued_series[-2:]
            change = Decimal(str(latest["value"])) - Decimal(str(previous["value"]))
            calculation_id = f"calculation:{iso3}:{item['key']}:latest-change"
            calculations.append(
                {
                    "calculation_id": calculation_id,
                    "name": f"{item['label']}最近一期变化",
                    "formula": "latest_value - previous_value",
                    "inputs": [previous, latest],
                    "result": float(change),
                    "unit": latest.get("unit"),
                    "evidence_ids": [evidence_id],
                }
            )
            trend_facts.append(
                FactItem(
                    fact_id=f"fact:{calculation_id}",
                    text=(
                        f"{item['label']}从 {previous['period']} 到 {latest['period']} 的确定性差值为 "
                        f"{_format_indicator_fact_value(change)} "
                        f"{_format_indicator_unit(latest.get('unit'))}。"
                    ),
                    evidence_ids=[evidence_id],
                    calculation_refs=[calculation_id],
                    origin="calculation",
                    verification_status="derived",
                )
            )
    policy_evidence = [
        EvidenceItem(
            evidence_id=f"document-version:{version.id}",
            kind="document",
            title=document.title or "官方政策文件",
            source_name=source.name,
            source_url=document.canonical_url or document.discovery_url,
            published_at=_iso_datetime(version.published_at or document.published_at),
            observed_at=_iso_datetime(document.last_seen_at),
            published_at_precision=_published_at_precision(version),
            evidence_locator={
                "document_version_id": version.id,
                "locator": (version.source_metadata or {}).get("evidence_locator", {}),
            },
            payload={"document_version_id": version.id, "document_type": document.document_type},
        )
        for version, document, source in policy_rows
    ]
    policy_facts = [
        FactItem(
            fact_id=f"fact:{item.evidence_id}",
            text=(
                f"已入库政策材料《{item.title}》，来源 {item.source_name}，"
                f"发布日期{_display_published_at(item.published_at, item.published_at_precision)}。"
            ),
            evidence_ids=[item.evidence_id],
        )
        for item in policy_evidence
    ]

    country = db.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == iso3,
        )
    )
    recent_evidence: list[EvidenceItem] = []
    event_evidence: list[EvidenceItem] = []
    event_titles: list[str] = []
    if country is not None:
        recent_rows = list(
            db.execute(
                select(DocumentVersion, Document, Source)
                .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    DocumentEntity.entity_id == country.id,
                    DocumentEntity.review_status == "confirmed",
                )
                .order_by(Document.published_at.desc(), DocumentVersion.id.desc())
                .limit(12)
            )
        )
        recent_evidence = [
            EvidenceItem(
                evidence_id=f"document-version:{version.id}",
                kind="document",
                title=document.title,
                source_name=source.name,
                source_url=document.canonical_url or document.discovery_url,
                published_at=_iso_datetime(version.published_at or document.published_at),
                observed_at=_iso_datetime(document.last_seen_at),
                published_at_precision=_published_at_precision(version),
                evidence_locator={"document_version_id": version.id},
                payload={"document_version_id": version.id, "document_type": document.document_type},
            )
            for version, document, source in recent_rows
        ]
        events = list(
            db.scalars(
                select(ResearchEvent)
                .where(
                    ResearchEvent.country_entity_id == country.id,
                    ResearchEvent.review_status == "reviewed",
                )
                .order_by(ResearchEvent.start_at.desc(), ResearchEvent.id.desc())
                .limit(5)
            )
        )
        event_titles = [event.title for event in events]
        event_ids = [event.id for event in events]
        if event_ids:
            mention_rows = list(
                db.execute(
                    select(EventMention, ResearchEvent, DocumentVersion, Document, Source)
                    .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
                    .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .join(Source, Source.id == Document.source_id)
                    .where(
                        EventMention.event_id.in_(event_ids),
                        EventMention.review_status == "confirmed",
                    )
                    .order_by(ResearchEvent.start_at.desc(), Source.name)
                    .limit(18)
                )
            )
            event_evidence = [
                EvidenceItem(
                    evidence_id=f"event-mention:{mention.id}",
                    kind="event_mention",
                    title=event.title,
                    source_name=source.name,
                    source_url=document.canonical_url or document.discovery_url,
                    published_at=_iso_datetime(version.published_at or document.published_at),
                    observed_at=_iso_datetime(document.last_seen_at),
                    published_at_precision=_published_at_precision(version),
                    evidence_locator=mention.evidence_locator,
                    payload={
                        "event_id": event.id,
                        "event_type": event.event_type,
                        "event_start_at": _iso_datetime(event.start_at),
                        "mention_summary": mention.mention_summary,
                        "reported_place": mention.source_reported_place,
                    },
                )
                for mention, event, version, document, source in mention_rows
            ]

    recent_facts = [
        FactItem(
            fact_id=f"fact:{item.evidence_id}",
            text=(
                f"已确认关联材料《{item.title}》，来源 {item.source_name}，"
                f"发布日期{_display_published_at(item.published_at, item.published_at_precision)}。"
            ),
            evidence_ids=[item.evidence_id],
        )
        for item in recent_evidence
    ]
    event_facts = [
        FactItem(
            fact_id=f"fact:{item.evidence_id}",
            text=(
                f"来源 {item.source_name} 对事件《{item.title}》的已确认提及为："
                f"{item.payload.get('mention_summary') or '来源仅提供事件提及，未形成可引用摘要。'}"
            ),
            evidence_ids=[item.evidence_id],
        )
        for item in event_evidence
    ]

    return [
        RegisteredAgentTool(
            name="country_basic_facts",
            description="读取该国首都、货币、官方语言和国土面积的固定版本基础档案。",
            result=ToolResult(
                capability="country_basic_facts",
                status="succeeded",
                summary="；".join(basic_fact_lines),
                evidence=[basic_fact_evidence],
                facts=basic_fact_facts,
                calculations=[],
                limitations=["参考注册表用于稳定基础国情，不替代政策、事件或动态统计证据。"],
            ),
        ),
        RegisteredAgentTool(
            name="country_snapshot",
            description="读取该国已入库的核心宏观指标及可复算时序变化。",
            result=ToolResult(
                capability="country_snapshot",
                status="succeeded" if indicator_facts else "insufficient_data",
                summary=(
                    f"{country_name}已入库指标：" + "；".join(indicator_lines)
                    if indicator_lines
                    else f"{country_name}当前没有可引用的指标数值。"
                ),
                evidence=indicator_evidence,
                facts=indicator_facts,
                calculations=calculations,
                limitations=[] if indicator_lines else ["指标目录存在，但当前库未加载可引用数值。"],
            ),
        ),
        RegisteredAgentTool(
            name="indicator_trend",
            description="读取结构化时序并执行确定性的同比、差值与趋势计算。",
            result=ToolResult(
                capability="indicator_trend",
                status="succeeded" if trend_facts else "insufficient_data",
                summary="；".join(item["name"] for item in calculations) or "当前没有可复算趋势。",
                evidence=indicator_evidence,
                facts=trend_facts,
                calculations=calculations,
                limitations=[] if calculations else ["当前时序不足以形成趋势计算。"],
            ),
        ),
        _country_trade_tool(db, iso3),
        RegisteredAgentTool(
            name="policy_timeline",
            description="读取该国已入库、可追溯的官方政策与法规材料元数据。",
            result=ToolResult(
                capability="policy_timeline",
                status="succeeded" if policy_facts else "insufficient_data",
                summary="；".join(item.title for item in policy_evidence) or "当前没有已入库的官方政策材料。",
                evidence=policy_evidence,
                facts=policy_facts,
                calculations=[],
                limitations=[] if policy_evidence else ["官方政策材料仍需补充。"],
            ),
        ),
        RegisteredAgentTool(
            name="event_evidence_compare",
            description="读取人工复核事件的多源报道，保留来源之间的数字与立场差异。",
            result=ToolResult(
                capability="event_evidence_compare",
                status="succeeded" if event_facts else "insufficient_data",
                summary="；".join(event_titles) or "当前没有达到复核门槛的事件证据。",
                evidence=event_evidence,
                facts=event_facts,
                calculations=[],
                limitations=[] if event_evidence else ["事件证据尚未达到可用于研判的复核门槛。"],
            ),
        ),
        RegisteredAgentTool(
            name="freshness_check",
            description="读取与该国实体已确认关联的近期材料清单。",
            result=ToolResult(
                capability="freshness_check",
                status="succeeded" if recent_facts else "insufficient_data",
                summary="；".join(item.title for item in recent_evidence) or "当前没有已确认关联的近期材料。",
                evidence=recent_evidence,
                facts=recent_facts,
                calculations=[],
                limitations=[] if recent_evidence else ["近期动态关联仍需人工复核。"],
            ),
        ),
    ]


def _country_trade_tool(db: Session, iso3: str) -> RegisteredAgentTool:
    rows = list(
        db.execute(
            select(StructuredObservation, StructuredObservationVersion)
            .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
            .join(
                StructuredObservationVersion,
                and_(
                    StructuredObservationVersion.observation_id == StructuredObservation.id,
                    StructuredObservationVersion.version_no == StructuredObservation.latest_version_no,
                ),
            )
            .where(
                StructuredDataset.dataset_key == "un-comtrade-goods-annual-hs",
                StructuredObservation.country_iso3 == iso3,
                StructuredObservation.metric_code == "trade_value",
                StructuredObservationVersion.value.is_not(None),
            )
            .order_by(
                StructuredObservation.trade_flow.desc(),
                StructuredObservation.partner_iso3,
                StructuredObservation.commodity_code,
                StructuredObservation.period,
            )
        )
    )
    grouped: dict[tuple[str, str, str], list[tuple[StructuredObservation, Any]]] = defaultdict(list)
    for observation, version in rows:
        key = (
            observation.trade_flow or "?",
            observation.partner_iso3 or "?",
            observation.commodity_code or "?",
        )
        grouped[key].append((observation, version))
    evidence: list[EvidenceItem] = []
    facts: list[FactItem] = []
    calculations: list[dict[str, Any]] = []
    for (flow, partner, commodity), series in grouped.items():
        if len(series) < 2:
            continue
        previous, latest = series[-2:]
        previous_observation, previous_version = previous
        latest_observation, latest_version = latest
        change = Decimal(str(latest_version.value)) - Decimal(str(previous_version.value))
        evidence_id = f"trade-series:{iso3}:{flow}:{partner}:{commodity}"
        calculation_id = f"calculation:{evidence_id}:latest-change"
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id,
                kind="structured_trade_series",
                title=f"HS {commodity} {flow} {partner} 贸易额时序",
                source_name="UN Comtrade Plus",
                source_url=latest_version.source_url,
                published_at=None,
                observed_at=_iso_datetime(latest_observation.last_seen_at),
                published_at_precision="year",
                retrieved_at=_iso_datetime(latest_observation.last_seen_at),
                evidence_locator={
                    "dataset_key": "un-comtrade-goods-annual-hs",
                    "flow": flow,
                    "partner_iso3": partner,
                    "commodity_code": commodity,
                    "periods": [previous_observation.period, latest_observation.period],
                },
                payload={
                    "series": [
                        {"period": item.period, "value": float(version.value)} for item, version in series
                    ]
                },
            )
        )
        calculations.append(
            {
                "calculation_id": calculation_id,
                "name": f"HS {commodity} {flow} {partner} 最近两期贸易额差值",
                "formula": "latest_trade_value - previous_trade_value",
                "inputs": [
                    {"period": previous_observation.period, "value": float(previous_version.value)},
                    {"period": latest_observation.period, "value": float(latest_version.value)},
                ],
                "result": float(change),
                "unit": latest_version.unit or "USD",
                "evidence_ids": [evidence_id],
            }
        )
        flow_label = "出口" if flow == "X" else "进口"
        facts.append(
            FactItem(
                fact_id=f"fact:{calculation_id}",
                text=(
                    f"HS {commodity} 对 {partner} 的{flow_label}贸易额从 "
                    f"{previous_observation.period} 年到 {latest_observation.period} 年变化 "
                    f"{_format_indicator_fact_value(change)} 美元。"
                ),
                evidence_ids=[evidence_id],
                calculation_refs=[calculation_id],
                origin="calculation",
                verification_status="derived",
            )
        )
        if len(facts) >= 6:
            break
    return RegisteredAgentTool(
        name="trade_trend",
        description="读取 UN Comtrade 贸易流并按流向、伙伴和 HS 商品执行确定性趋势计算。",
        result=ToolResult(
            capability="trade_trend",
            status="succeeded" if facts else "insufficient_data",
            summary=f"形成 {len(facts)} 组可复算贸易时序。" if facts else "当前没有可复算贸易时序。",
            evidence=evidence,
            facts=facts,
            calculations=calculations,
            limitations=[] if facts else ["当前 Comtrade 记录不足以形成两期贸易趋势。"],
        ),
    )


def _run_assistant_request(
    db: Session,
    payload: AssistantQuestionPayload,
    *,
    output_type: str = "research_analysis",
    requested_by: int | None = None,
) -> dict[str, Any]:
    agent_run = _create_assistant_run(db, payload, requested_by=requested_by)
    try:
        return _execute_assistant_run(
            db,
            payload=payload,
            run_id=agent_run.id,
            output_type=output_type,
        )
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _create_assistant_run(
    db: Session,
    payload: AssistantQuestionPayload,
    *,
    requested_by: int | None = None,
) -> AgentRun:
    scope_type, scope_key, scope_context = _assistant_scope(payload.context)
    scope_context["references"] = _validated_country_references(db, payload)
    conversation_id = payload.conversation_id or str(uuid.uuid4())
    previous_statement = select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    current_case_id = _assistant_case_id(scope_type, scope_key, scope_context)
    if current_case_id is not None:
        if payload.online_mode == "on":
            raise HTTPException(status_code=422, detail="项目研究不支持联网补充，请使用国别空间库内证据")
        payload.online_mode = "off"
        if payload.references:
            raise HTTPException(status_code=422, detail="请先在项目资料库审阅采用，再引用项目资料")

    if current_case_id is None:
        owner_filter = AgentRun.requested_by == requested_by
        if not _reader_identity_initialized(db):
            owner_filter = or_(owner_filter, AgentRun.requested_by.is_(None))
        previous_statement = previous_statement.where(owner_filter)
    previous = db.scalar(previous_statement.order_by(AgentRun.id.desc()).limit(1))
    if previous is not None and (previous.scope_type, previous.scope_key) != (scope_type, scope_key):
        raise HTTPException(status_code=409, detail="conversation belongs to a different assistant scope")

    iso3 = (payload.context.country_iso3 or "COD").upper()
    settings = get_settings()
    started_at = datetime.now(UTC)
    agent_run = AgentRun(
        requested_by=requested_by,
        country_iso3=iso3,
        conversation_id=conversation_id,
        scope_type=scope_type,
        scope_key=scope_key,
        scope_context=scope_context,
        question=payload.question.strip(),
        workflow=("evidence_synthesis" if settings.agent_runtime == "evidence_only" else "bounded_agent"),
        runtime=settings.agent_runtime,
        model_name=settings.agent_model or None,
        status="running",
        plan={},
        tool_trace=[],
        evidence=[],
        artifact={},
        started_at=started_at,
    )
    db.add(agent_run)
    db.commit()
    db.refresh(agent_run)
    return agent_run


def _execute_assistant_run(
    db: Session,
    *,
    payload: AssistantQuestionPayload,
    run_id: int,
    output_type: str = "research_analysis",
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    agent_run = db.get(AgentRun, run_id)
    if agent_run is None:
        raise AgentRuntimeError("assistant run no longer exists")
    iso3 = agent_run.country_iso3
    scope_type = agent_run.scope_type
    scope_key = agent_run.scope_key
    scope_context = agent_run.scope_context
    country_name = _COUNTRY_DISPLAY.get(iso3, {}).get("name_zh") or _country_name(iso3)
    tools = _assistant_tools(db, payload.context, country_name=country_name)
    references = _validated_country_references(db, payload)
    if references:
        evidence = [
            EvidenceItem(
                evidence_id=f"selected:{index}",
                kind=ref["type"],
                title=ref["title"],
                source_name=ref["source_name"],
                source_url=ref.get("source_url"),
                published_at=ref.get("published_at"),
                observed_at=ref.get("observed_at"),
                published_at_precision=ref.get("published_at_precision", "unknown"),
                evidence_locator={
                    "type": ref["type"],
                    "id": ref["id"],
                    "snapshot_id": ref.get("snapshot_id"),
                    "field": ref.get("field"),
                    "start": ref.get("start"),
                },
                payload=ref,
            )
            for index, ref in enumerate(references)
        ]
        facts = [
            FactItem(
                fact_id=f"fact:{item.evidence_id}", text=item.payload["text"], evidence_ids=[item.evidence_id]
            )
            for item in evidence
        ]
        tools.insert(
            0, _tool("selected_context", "读取用户所选原始版本与选文，围绕引用回答问题。", facts, evidence)
        )
    history = _assistant_history(db, agent_run)
    settings = get_settings()
    try:
        result = run_country_agent(
            settings=settings,
            request=AgentRequest(
                country_iso3=iso3,
                country_name=country_name,
                question=payload.question.strip(),
                output_type=output_type,
                scope_type=scope_type,
                scope_key=scope_key,
                scope_context=scope_context,
                conversation_history=history,
                answer_mode=payload.answer_mode,
            ),
            tools=tools,
            fallback_related_questions=_assistant_related_questions(scope_type),
            progress=progress,
        )
        artifact = result.artifact
        with _ASSISTANT_RUN_PROGRESS_LOCK:
            canceled = run_id in _ASSISTANT_CANCEL_REQUESTS
        if canceled:
            raise AgentRuntimeError("用户已停止本轮研究。")
        artifact.proposed_actions = _proposed_assistant_actions(
            db,
            context=payload.context,
            run_id=agent_run.id,
            question=payload.question.strip(),
        )
        _append_live_search(
            artifact,
            settings=settings,
            question=payload.question.strip(),
            scope_context=scope_context,
            online_mode=payload.online_mode,
            progress=progress,
        )
    except AgentRuntimeError as exc:
        with _ASSISTANT_RUN_PROGRESS_LOCK:
            canceled = run_id in _ASSISTANT_CANCEL_REQUESTS
            _ASSISTANT_CANCEL_REQUESTS.discard(run_id)
        _fail_assistant_run(
            db,
            agent_run,
            "user_canceled" if canceled else "agent_runtime_failed",
            str(exc),
        )
        raise

    agent_run.workflow = result.plan.workflow
    agent_run.plan = result.plan.model_dump(mode="json")
    agent_run.tool_trace = artifact.tool_trace
    agent_run.evidence = [item.model_dump(mode="json") for item in artifact.citations]
    agent_run.artifact = artifact.model_dump(mode="json")
    agent_run.status = "succeeded"
    agent_run.finished_at = datetime.now(UTC)
    db.commit()
    with _ASSISTANT_RUN_PROGRESS_LOCK:
        _ASSISTANT_CANCEL_REQUESTS.discard(run_id)
    response = _assistant_response(agent_run, artifact)
    if progress is not None:
        progress(
            "evidence.validated",
            {
                "stage": "final_artifact",
                "evidence_count": len(artifact.citations),
                "section_count": len(artifact.sections),
                "source_mode": artifact.source_mode,
            },
        )
        citations = {item.evidence_id: item for item in artifact.citations}
        for section_index, section in enumerate(artifact.sections):
            evidence_ids = {evidence_id for claim in section.claims for evidence_id in claim.evidence_ids}
            progress(
                "answer.section",
                {
                    "index": section_index,
                    "section": {
                        **section.model_dump(mode="json"),
                        "claims": [{**claim.model_dump(mode="json"), "text": ""} for claim in section.claims],
                    },
                    "citations": [
                        citations[evidence_id].model_dump(mode="json")
                        for evidence_id in evidence_ids
                        if evidence_id in citations
                    ],
                },
            )
            for claim_index, claim in enumerate(section.claims):
                for offset in range(0, len(claim.text), 12):
                    progress(
                        "answer.delta",
                        {
                            "index": section_index,
                            "claim_index": claim_index,
                            "delta": claim.text[offset : offset + 12],
                        },
                    )
                    time.sleep(0.025)
    return response


def _fail_assistant_run(
    db: Session,
    agent_run: AgentRun,
    error_code: str,
    error_message: str,
) -> None:
    agent_run.status = "failed"
    agent_run.error_code = error_code
    agent_run.error_message = error_message
    agent_run.finished_at = datetime.now(UTC)
    db.commit()


def _assistant_event_stream(
    worker_sessions: sessionmaker[Session],
    *,
    payload: AssistantQuestionPayload,
    run_id: int,
    conversation_id: str,
    scope_type: str,
    scope_key: str,
    runtime: str,
) -> Iterator[str]:
    event_queue: Queue[dict[str, Any]] = Queue(maxsize=ASSISTANT_STREAM_QUEUE_SIZE)
    sequence = 0
    sequence_lock = Lock()

    def emit(event: str, event_payload: dict[str, Any]) -> None:
        nonlocal sequence
        with sequence_lock:
            sequence += 1
            envelope = {
                "run_id": run_id,
                "conversation_id": conversation_id,
                "sequence": sequence,
                "scope": {"type": scope_type, "key": scope_key},
                "payload": event_payload,
            }
        event_queue.put({"event": event, "envelope": envelope})
        with _ASSISTANT_RUN_PROGRESS_LOCK:
            if event in _ASSISTANT_STREAM_TERMINAL_EVENTS:
                _ASSISTANT_RUN_PROGRESS.pop(run_id, None)
            else:
                _ASSISTANT_RUN_PROGRESS[run_id] = {
                    "event": event,
                    "sequence": envelope["sequence"],
                    "payload": event_payload,
                    "updated_at": _iso_datetime(datetime.now(UTC)),
                }

    def worker() -> None:
        with worker_sessions() as worker_db:
            try:
                response = _execute_assistant_run(
                    worker_db,
                    payload=payload,
                    run_id=run_id,
                    progress=emit,
                )
            except AgentRuntimeError as exc:
                emit(
                    "run.failed",
                    {"error_code": "agent_runtime_failed", "message": str(exc)},
                )
            except Exception:
                worker_db.rollback()
                agent_run = worker_db.get(AgentRun, run_id)
                if agent_run is not None and agent_run.status == "running":
                    _fail_assistant_run(
                        worker_db,
                        agent_run,
                        "assistant_internal_error",
                        "assistant run failed unexpectedly",
                    )
                emit(
                    "run.failed",
                    {
                        "error_code": "assistant_internal_error",
                        "message": "AI 运行失败；库内未写入未完成答案。",
                    },
                )
            else:
                emit("run.completed", {"response": response})

    emit(
        "run.started",
        {
            "runtime": runtime,
            "stage": "understand_question",
            "message": "已锁定当前研究范围，正在形成受控研究计划。",
        },
    )
    thread = Thread(target=copy_context().run, args=(worker,), name=f"assistant-run-{run_id}", daemon=True)
    thread.start()
    while True:
        try:
            item = event_queue.get(timeout=ASSISTANT_STREAM_HEARTBEAT_SECONDS)
        except Empty:
            yield f": heartbeat {_iso_datetime(datetime.now(UTC))}\n\n"
            continue
        event = item["event"]
        envelope = item["envelope"]
        yield (
            f"id: {envelope['sequence']}\n"
            f"event: {event}\n"
            f"data: {json.dumps(envelope, ensure_ascii=False, default=str)}\n\n"
        )
        if event in _ASSISTANT_STREAM_TERMINAL_EVENTS:
            break


def _assistant_run_status(agent_run: AgentRun) -> dict[str, Any]:
    with _ASSISTANT_RUN_PROGRESS_LOCK:
        progress = dict(_ASSISTANT_RUN_PROGRESS.get(agent_run.id) or {})
    response = None
    if agent_run.status == "succeeded" and agent_run.artifact:
        artifact = AnswerArtifact.model_validate(agent_run.artifact)
        response = _assistant_response(agent_run, artifact)
    return {
        "run_id": agent_run.id,
        "conversation_id": agent_run.conversation_id,
        "scope_type": agent_run.scope_type,
        "scope_key": agent_run.scope_key,
        "status": agent_run.status,
        "runtime": agent_run.runtime,
        "started_at": agent_run.started_at,
        "finished_at": agent_run.finished_at,
        "progress": progress or None,
        "response": response,
        "error": (
            {"code": agent_run.error_code, "message": agent_run.error_message}
            if agent_run.status == "failed"
            else None
        ),
    }


def _assistant_scope(context: AssistantContextPayload) -> tuple[str, str, dict[str, Any]]:
    values = context.model_dump(mode="json", exclude_none=True)
    if context.space == "country":
        key = context.country_iso3 or "catalog"
    elif context.space == "event":
        key = str(context.event_id) if context.event_id is not None else "overview"
    elif context.space == "topic":
        key = str(context.research_case_id) if context.research_case_id is not None else "overview"
    elif context.space == "capability":
        key = (
            f"run:{context.capability_run_id}"
            if context.capability_run_id
            else f"config:{context.capability_config_id}"
            if context.capability_config_id
            else "overview"
        )
    else:
        key = "catalog" if context.space == "resource" else "readiness"
    return context.space, key, values


def _assistant_history(
    db: Session,
    current_run: AgentRun,
) -> list[dict[str, str]]:
    statement = select(AgentRun).where(
        AgentRun.conversation_id == current_run.conversation_id,
        AgentRun.scope_type == current_run.scope_type,
        AgentRun.scope_key == current_run.scope_key,
        AgentRun.status == "succeeded",
        AgentRun.id < current_run.id,
    )
    if (
        _assistant_case_id(
            current_run.scope_type,
            current_run.scope_key,
            current_run.scope_context or {},
        )
        is None
    ):
        owner_filter = AgentRun.requested_by == current_run.requested_by
        if not _reader_identity_initialized(db):
            owner_filter = or_(owner_filter, AgentRun.requested_by.is_(None))
        statement = statement.where(owner_filter)
    runs = list(db.scalars(statement.order_by(AgentRun.id.desc()).limit(12)))
    return [
        {"question": run.question, "answer": artifact_markdown_from_dict(run.artifact)[:3000]}
        for run in reversed(runs)
    ]


def _assistant_tools(
    db: Session,
    context: AssistantContextPayload,
    *,
    country_name: str,
) -> list[RegisteredAgentTool]:
    iso3 = context.country_iso3 or "COD"
    if context.space == "country":
        if context.country_iso3 is None:
            return _country_catalog_agent_tools(db)
        indicators, _ = _country_key_indicators(db, iso3)
        tools = _country_agent_tools(
            db,
            iso3=iso3,
            country_name=country_name,
            indicators=indicators,
            policy_rows=_country_policy_documents(db, iso3),
        )
        return tools + (_capability_agent_tools(db, context) if context.capability_config_ids else [])
    if context.space == "event":
        tools = (
            _event_agent_tools(db, context.event_id) if context.event_id else _event_overview_agent_tools(db)
        )
        return tools + (_capability_agent_tools(db, context) if context.capability_config_ids else [])
    if context.space == "topic":
        tools = (
            _topic_agent_tools(db, context.research_case_id)
            if context.research_case_id
            else _topic_overview_agent_tools(db)
        )
        return tools + (_capability_agent_tools(db, context) if context.capability_config_ids else [])
    if context.space == "capability":
        return _capability_agent_tools(db, context)
    if context.space == "resource":
        return _resource_agent_tools(db)
    return _system_agent_tools(db)


def _country_catalog_agent_tools(db: Session) -> list[RegisteredAgentTool]:
    payload = get_country_catalog(db)
    summary = payload["summary"]
    source = payload["source"]
    evidence = EvidenceItem(
        evidence_id="country-catalog:un-m49",
        kind="country_catalog",
        title="联合国 M49 国别目录与国别智枢覆盖状态",
        source_name=source["catalog"],
        source_url=source["catalog_url"],
        published_at=None,
        observed_at=_iso_datetime(summary["latest_collection_at"]),
        retrieved_at=_iso_datetime(summary["latest_collection_at"]),
        published_at_precision="unknown",
        evidence_locator={"catalog": "data/country_catalog.json"},
        payload=summary,
    )
    facts = [
        FactItem(
            fact_id="fact:country-catalog:membership",
            text=(
                f"全球目录收录 {summary['country_count']} 个国家和观察员国，"
                f"其中 {source['member_count']} 个联合国会员国、{source['observer_count']} 个观察员国。"
            ),
            evidence_ids=[evidence.evidence_id],
        ),
        FactItem(
            fact_id="fact:country-catalog:coverage",
            text=(
                f"当前有 {summary['deep_ready_count']} 个深度样板、"
                f"{summary['data_partial_count']} 个仅有部分结构化数据的国家；"
                "刚果（金）是唯一深度样板。"
            ),
            evidence_ids=[evidence.evidence_id],
        ),
    ]
    return [
        _tool(
            "country_snapshot",
            "读取全球国别目录与当前深度数据覆盖状态。",
            facts,
            [evidence],
        )
    ]


def _event_agent_tools(db: Session, event_id: int) -> list[RegisteredAgentTool]:
    event = db.get(ResearchEvent, event_id)
    if event is None or event.review_status != "reviewed":
        raise HTTPException(status_code=404, detail="reviewed event not found")
    rows = list(
        db.execute(
            select(EventMention, DocumentVersion, Document, Source)
            .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(EventMention.event_id == event_id, EventMention.review_status == "confirmed")
            .order_by(Source.name, EventMention.id)
        )
    )
    evidence = [
        EvidenceItem(
            evidence_id=f"event-mention:{mention.id}",
            kind="event_mention",
            title=event.title,
            source_name=source.name,
            source_url=document.canonical_url or document.discovery_url,
            published_at=_iso_datetime(version.published_at or document.published_at),
            observed_at=_iso_datetime(document.last_seen_at),
            retrieved_at=_iso_datetime(document.last_seen_at),
            published_at_precision=_published_at_precision(version),
            evidence_locator=mention.evidence_locator,
            payload={
                "document_version_id": version.id,
                "mention_summary": mention.mention_summary,
                "reported_start_at": _iso_datetime(mention.source_reported_start_at),
            },
        )
        for mention, version, document, source in rows
    ]
    mention_facts = [
        FactItem(
            fact_id=f"fact:{item.evidence_id}",
            text=(
                f"来源 {item.source_name} 对事件《{event.title}》的已确认提及为："
                f"{item.payload.get('mention_summary') or '仅确认事件提及，未登记摘要。'}"
            ),
            evidence_ids=[item.evidence_id],
        )
        for item in evidence
    ]
    mention_by_id = {mention.id: item for (mention, *_), item in zip(rows, evidence, strict=True)}
    claims = list(
        db.scalars(
            select(EvidenceClaim)
            .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
            .where(
                EventMention.event_id == event_id,
                EvidenceClaim.review_status == "confirmed",
            )
            .order_by(EvidenceClaim.comparison_key, EvidenceClaim.id)
        )
    )
    claim_facts = []
    for claim in claims:
        evidence_item = mention_by_id.get(claim.event_mention_id)
        if evidence_item is None:
            continue
        value = claim.value_text or (
            f"{_number(claim.numeric_value)} {claim.unit or ''}"
            if claim.numeric_value is not None
            else "未登记"
        )
        claim_facts.append(
            FactItem(
                fact_id=f"fact:evidence-claim:{claim.id}",
                text=f"{evidence_item.source_name} 对“{claim.comparison_key}”的已确认表述为：{value}。",
                evidence_ids=[evidence_item.evidence_id],
            )
        )
    event_fact = []
    if evidence:
        event_fact = [
            FactItem(
                fact_id=f"fact:event:{event.id}:timeline",
                text=(
                    f"已复核事件《{event.title}》的日期为 "
                    f"{_display_published_at(_iso_datetime(event.start_at), event.date_precision)}。"
                ),
                evidence_ids=[item.evidence_id for item in evidence],
            )
        ]
    source_count = len({source.id for _, _, _, source in rows})
    comparison_count = len({claim.comparison_key for claim in claims})
    gap_evidence = EvidenceItem(
        evidence_id=f"calculation:event:{event.id}:gap",
        kind="calculation",
        title="事件证据覆盖计算",
        source_name="国别智枢确定性计算",
        source_url=None,
        published_at=None,
        observed_at=_iso_datetime(datetime.now(UTC)),
        retrieved_at=_iso_datetime(datetime.now(UTC)),
        evidence_locator={"event_id": event.id},
        review_status="confirmed",
        origin="calculation",
        verification_status="derived",
        payload={"source_count": source_count, "comparison_group_count": comparison_count},
    )
    gap_fact = FactItem(
        fact_id=f"fact:{gap_evidence.evidence_id}",
        text=(
            f"事件《{event.title}》当前有 {source_count} 个不同确认来源、{comparison_count} 个可比较主张组。"
        ),
        evidence_ids=[gap_evidence.evidence_id],
        origin="calculation",
        verification_status="derived",
    )
    relation_rows = list(
        db.execute(
            select(EventRelation, ResearchEvent)
            .join(
                ResearchEvent,
                or_(
                    ResearchEvent.id == EventRelation.source_event_id,
                    ResearchEvent.id == EventRelation.target_event_id,
                ),
            )
            .where(
                or_(
                    EventRelation.source_event_id == event_id,
                    EventRelation.target_event_id == event_id,
                ),
                ResearchEvent.id != event_id,
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(EventRelation.id)
        )
    )
    relation_evidence = [
        EvidenceItem(
            evidence_id=f"event-relation:{relation.id}",
            kind="event_relation",
            title=other_event.title,
            source_name="国别智枢事件关系登记",
            source_url=None,
            published_at=_iso_datetime(other_event.start_at),
            observed_at=None,
            retrieved_at=None,
            published_at_precision=other_event.date_precision,
            evidence_locator={"relation_id": relation.id},
            review_status="confirmed",
            origin="database",
            verification_status="reviewed",
            payload={
                "event_id": other_event.id,
                "relation_type": relation.relation_type,
                "direction": "outgoing" if relation.source_event_id == event_id else "incoming",
                "note": relation.note,
            },
        )
        for relation, other_event in relation_rows
    ]
    relation_facts = [
        FactItem(
            fact_id=f"fact:{item.evidence_id}",
            text=(
                f"事件《{event.title}》与已复核事件《{item.title}》登记为"
                f"“{item.payload['relation_type']}”关系"
                f"（{item.payload['direction']}）。"
            ),
            evidence_ids=[item.evidence_id],
        )
        for item in relation_evidence
    ]
    return [
        _tool("event_timeline", "读取当前 reviewed 事件的日期与确认来源。", event_fact, evidence),
        _tool(
            "event_evidence_compare",
            "并列当前事件的已确认来源提及。",
            mention_facts,
            evidence,
        ),
        _tool("claim_difference", "读取按 comparison_key 登记的来源主张差异。", claim_facts, evidence),
        _tool(
            "related_events",
            "读取当前事件与其他 reviewed 事件的已登记关系。",
            relation_facts,
            relation_evidence,
        ),
        _tool(
            "event_evidence_gap",
            "计算当前事件距离三源和主张差异门槛的缺口。",
            [gap_fact],
            [gap_evidence],
            calculations=[gap_evidence.payload],
        ),
    ]


def _event_overview_agent_tools(db: Session) -> list[RegisteredAgentTool]:
    events = list(
        db.scalars(
            select(ResearchEvent)
            .where(ResearchEvent.review_status == "reviewed")
            .order_by(ResearchEvent.start_at.desc(), ResearchEvent.id.desc())
            .limit(5)
        )
    )
    facts: list[FactItem] = []
    evidence: list[EvidenceItem] = []
    for event in events:
        tools = _event_agent_tools(db, event.id)
        timeline = next(item for item in tools if item.name == "event_timeline")
        facts.extend(timeline.result.facts)
        evidence.extend(timeline.result.evidence)
    return [
        _tool("event_timeline", "读取最近 reviewed 事件及其确认来源。", facts, evidence),
        _tool(
            "event_evidence_gap",
            "解释事件空间为何为空或哪些事件尚未达到来源门槛。",
            facts,
            evidence,
            limitations=[] if facts else ["当前数据库没有 reviewed 事件。"],
        ),
    ]


def _project_structured_agent_tool(db: Session, research_case: ResearchCase) -> RegisteredAgentTool:
    from app.services.project_candidates import adopted_observations

    scope = research_case.scope or {}
    countries = scope.get("country_iso3s") or ([scope["country_iso3"]] if scope.get("country_iso3") else None)
    rows, locators = adopted_observations(db, research_case.id, countries)
    evidence, facts = [], []
    for observation, version, dataset in rows:
        evidence_id = f"project-observation:{research_case.id}:{version.id}"
        payload = {
            "country_iso3": observation.country_iso3,
            "indicator_code": observation.indicator_code,
            "metric_code": observation.metric_code,
            "period": observation.period,
            "frequency": observation.frequency,
            "value_text": str(version.value) if version.value is not None else None,
            "missing_reason": version.missing_reason,
            "unit": version.unit,
            "currency": version.currency,
            "price_basis": version.price_basis,
            "partner_iso3": observation.partner_iso3,
            "commodity_code": observation.commodity_code,
            "flow": observation.trade_flow,
            "commodity_classification": observation.commodity_classification,
            "partner2_code": observation.partner2_code,
            "customs_code": observation.customs_code,
            "mot_code": observation.mot_code,
            "source_status": version.source_status,
            "quality_flags": version.quality_flags,
        }
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id,
                kind="structured_observation",
                title=(
                    f"{observation.country_iso3} · "
                    f"{observation.indicator_code or observation.metric_code} · {observation.period}"
                ),
                source_name=dataset.name,
                source_url=version.source_url,
                published_at=None,
                observed_at=_iso_datetime(observation.last_seen_at),
                evidence_locator={**locators[version.id], "research_case_id": research_case.id},
                payload=payload,
            )
        )
        value = (
            str(version.value)
            if version.value is not None
            else f"缺失（{version.missing_reason or '未注明原因'}）"
        )
        facts.append(
            FactItem(
                fact_id=f"fact:{evidence_id}",
                evidence_ids=[evidence_id],
                text=(
                    f"{observation.country_iso3} {observation.period} 期 "
                    f"{observation.indicator_code or observation.metric_code}：{value} {version.unit or ''}。"
                ),
            )
        )
    return _tool(
        "project_structured_observations",
        "读取项目已采用且当前可引用的固定快照观测。",
        facts,
        evidence,
        limitations=["仅列示采用版本的原始观测；不同口径不直接合并，不据单期推断趋势。"],
    )


def _topic_agent_tools(db: Session, case_id: int) -> list[RegisteredAgentTool]:
    research_case = db.get(ResearchCase, case_id)
    if research_case is None or research_case.status != "active":
        raise HTTPException(status_code=404, detail="active research case not found")
    rows = list(
        db.execute(
            select(ResearchCaseDocument, DocumentVersion, Document, Source)
            .join(DocumentVersion, DocumentVersion.id == ResearchCaseDocument.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(ResearchCaseDocument.research_case_id == case_id)
            .order_by(DocumentVersion.id.desc())
        )
    )
    decisions = project_workflow.adoption_decisions(db, case_id)
    rows = [row for row in rows if project_workflow.adopted_document(db, decisions, row[1].id)]
    confirmed_ids = set(
        db.scalars(
            select(DocumentEntity.document_version_id).where(
                DocumentEntity.document_version_id.in_([row[1].id for row in rows] or [-1]),
                DocumentEntity.review_status == "confirmed",
            )
        )
    ) | set(
        db.scalars(
            select(EventMention.document_version_id).where(
                EventMention.document_version_id.in_([row[1].id for row in rows] or [-1]),
                EventMention.review_status == "confirmed",
            )
        )
    )
    evidence = [
        EvidenceItem(
            evidence_id=f"document-version:{version.id}",
            kind="document",
            title=document.title,
            source_name=source.name,
            source_url=document.canonical_url or document.discovery_url,
            published_at=_iso_datetime(version.published_at or document.published_at),
            observed_at=_iso_datetime(document.last_seen_at),
            retrieved_at=_iso_datetime(document.last_seen_at),
            published_at_precision=_published_at_precision(version),
            evidence_locator={"document_version_id": version.id, "topic_id": case_id},
            payload={"usage_type": link.usage_type, "document_type": document.document_type},
        )
        for link, version, document, source in rows
        if version.id in confirmed_ids
    ]
    material_facts = [
        FactItem(
            fact_id=f"fact:{item.evidence_id}",
            text=(
                f"专题《{research_case.title}》已确认材料《{item.title}》，来源 {item.source_name}，"
                f"发布日期{_display_published_at(item.published_at, item.published_at_precision)}。"
            ),
            evidence_ids=[item.evidence_id],
        )
        for item in evidence
    ]
    event_rows = list(
        db.scalars(
            select(ResearchEvent)
            .join(ResearchCaseEvent, ResearchCaseEvent.event_id == ResearchEvent.id)
            .where(
                ResearchCaseEvent.research_case_id == case_id,
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(ResearchEvent.start_at.is_(None), ResearchEvent.start_at, ResearchEvent.id)
        )
    )
    event_ids = [event.id for event in event_rows]
    mention_rows = list(
        db.execute(
            select(EventMention, DocumentVersion, Document, Source)
            .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(
                EventMention.event_id.in_(event_ids or [-1]),
                EventMention.review_status == "confirmed",
            )
            .order_by(EventMention.event_id, EventMention.id)
        )
    )
    event_evidence_by_id: dict[int, list[EvidenceItem]] = defaultdict(list)
    for mention, version, document, source in mention_rows:
        if not project_workflow.is_adopted(
            decisions, "event_mention", mention.id
        ) or not project_workflow.adopted_document(db, decisions, version.id):
            continue

        event_evidence_by_id[mention.event_id].append(
            EvidenceItem(
                evidence_id=f"event-mention:{mention.id}",
                kind="event_mention",
                title=document.title,
                source_name=source.name,
                source_url=document.canonical_url or document.discovery_url,
                published_at=_iso_datetime(version.published_at or document.published_at),
                observed_at=_iso_datetime(document.last_seen_at),
                retrieved_at=_iso_datetime(document.last_seen_at),
                published_at_precision=_published_at_precision(version),
                evidence_locator={
                    "event_mention_id": mention.id,
                    "event_id": mention.event_id,
                    "document_version_id": version.id,
                    **(mention.evidence_locator or {}),
                },
                review_status="confirmed",
                verification_status="reviewed",
                payload={"mention_summary": mention.mention_summary},
            )
        )
    event_evidence = [item for items in event_evidence_by_id.values() for item in items]
    event_facts = [
        FactItem(
            fact_id=f"fact:topic:{case_id}:event:{event.id}",
            text=f"专题《{research_case.title}》关联 reviewed 事件《{event.title}》。",
            evidence_ids=[item.evidence_id for item in event_evidence_by_id[event.id]],
        )
        for event in event_rows
        if event_evidence_by_id[event.id]
    ]
    gap_evidence = EvidenceItem(
        evidence_id=f"calculation:topic:{case_id}:gap",
        kind="calculation",
        title="专题证据覆盖计算",
        source_name="国别智枢确定性计算",
        source_url=None,
        published_at=None,
        observed_at=_iso_datetime(datetime.now(UTC)),
        retrieved_at=_iso_datetime(datetime.now(UTC)),
        evidence_locator={"research_case_id": case_id},
        origin="calculation",
        verification_status="derived",
        payload={
            "confirmed_materials": len(evidence),
            "reviewed_events": sum(bool(items) for items in event_evidence_by_id.values()),
        },
    )
    gap_fact = FactItem(
        fact_id=f"fact:{gap_evidence.evidence_id}",
        text=(
            f"专题《{research_case.title}》当前有 {len(evidence)} 份已确认材料、"
            f"{sum(bool(items) for items in event_evidence_by_id.values())} 个 reviewed 事件。"
        ),
        evidence_ids=[gap_evidence.evidence_id],
        origin="calculation",
        verification_status="derived",
    )
    return [
        _project_structured_agent_tool(db, research_case),
        _tool("topic_material_search", "按当前专题范围读取已确认材料。", material_facts, evidence),
        _tool("topic_evidence_summary", "汇总专题已确认材料，不使用候选材料。", material_facts, evidence),
        _tool(
            "topic_event_overview",
            "读取项目关联的 reviewed 事件及其真实 confirmed EventMention。",
            event_facts,
            event_evidence,
            limitations=[]
            if event_facts
            else ["项目事件缺少 confirmed EventMention，不使用项目其他材料代替。"],
        ),
        _tool(
            "topic_gap_analysis",
            "计算专题确认材料与 reviewed 事件覆盖缺口。",
            [gap_fact],
            [gap_evidence],
            calculations=[gap_evidence.payload],
        ),
        _tool(
            "topic_digest_preview",
            "预览仅由已确认材料组成的专题摘要，不创建运行。",
            material_facts,
            evidence,
            limitations=["该结果是问答预览；保存周报需要确认后运行专题能力。"],
        ),
    ]


def _topic_overview_agent_tools(db: Session) -> list[RegisteredAgentTool]:
    cases = list(
        db.scalars(
            select(ResearchCase)
            .where(ResearchCase.status == "active")
            .order_by(ResearchCase.updated_at.desc(), ResearchCase.id.desc())
            .limit(5)
        )
    )
    facts: list[FactItem] = []
    evidence: list[EvidenceItem] = []
    for research_case in cases:
        tools = _topic_agent_tools(db, research_case.id)
        gap = next(item for item in tools if item.name == "topic_gap_analysis")
        facts.extend(gap.result.facts)
        evidence.extend(gap.result.evidence)
    return [
        _tool("topic_evidence_summary", "读取活动专题及已确认材料覆盖。", facts, evidence),
        _tool(
            "topic_gap_analysis",
            "解释专题空间材料与事件缺口。",
            facts,
            evidence,
            limitations=[] if facts else ["当前数据库没有活动专题。"],
        ),
    ]


def _capability_agent_tools(
    db: Session,
    context: AssistantContextPayload,
) -> list[RegisteredAgentTool]:
    run = db.get(CapabilityRun, context.capability_run_id) if context.capability_run_id else None
    configs = [
        config
        for config_id in context.capability_config_ids
        if (config := db.get(CapabilityConfig, config_id)) is not None
    ]
    if run is not None and not configs:
        config = db.get(CapabilityConfig, run.config_id)
        configs = [config] if config is not None else []
    if not configs:
        config = db.scalar(
            select(CapabilityConfig)
            .where(CapabilityConfig.status == "active")
            .order_by(CapabilityConfig.id)
            .limit(1)
        )
        configs = [config] if config is not None else []
    if run is None and len(configs) == 1:
        run = db.scalar(
            select(CapabilityRun)
            .where(CapabilityRun.config_id == configs[0].id)
            .order_by(CapabilityRun.id.desc())
            .limit(1)
        )
    evidence: list[EvidenceItem] = []
    facts: list[FactItem] = []
    for position, config in enumerate(configs, start=1):
        template = db.get(CapabilityTemplate, config.template_id)
        allowed_fields = list((template.input_schema or {}).get("allowed_fields") or []) if template else []
        output_type = (
            str((template.output_schema or {}).get("type") or "带引用候选产物")
            if template
            else "带引用候选产物"
        )
        evidence_id = f"capability-config:{config.id}"
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id,
                kind="capability_config",
                title=config.name,
                source_name="国别智枢内部能力配置",
                source_url=None,
                published_at=None,
                observed_at=_iso_datetime(config.updated_at),
                retrieved_at=_iso_datetime(datetime.now(UTC)),
                evidence_locator={"capability_config_id": config.id},
                origin="database",
                verification_status="derived",
                payload={
                    "status": config.status,
                    "config": config.config,
                    "allowed_inputs": allowed_fields,
                    "output_type": output_type,
                },
            )
        )
        facts.append(
            FactItem(
                fact_id=f"fact:{evidence_id}",
                text=f"本轮第 {position} 项能力《{config.name}》当前状态为 {config.status}。",
                evidence_ids=[evidence_id],
                verification_status="derived",
            )
        )
        facts.append(
            FactItem(
                fact_id=f"fact:{evidence_id}:io",
                text=(
                    f"《{config.name}》允许输入字段为"
                    f"{('、'.join(allowed_fields) if allowed_fields else '当前范围')}，"
                    f"输出类型为 {output_type}。"
                ),
                evidence_ids=[evidence_id],
                verification_status="derived",
            )
        )
    if run is not None:
        evidence_id = f"capability-run:{run.id}"
        evidence.append(
            EvidenceItem(
                evidence_id=evidence_id,
                kind="capability_run",
                title=f"能力运行 #{run.id}",
                source_name="国别智枢内部运行记录",
                source_url=None,
                published_at=None,
                observed_at=_iso_datetime(run.finished_at or run.started_at),
                retrieved_at=_iso_datetime(datetime.now(UTC)),
                evidence_locator={"capability_run_id": run.id},
                origin="database",
                verification_status="derived",
                payload={"status": run.status, "error_code": run.error_code},
            )
        )
        facts.append(
            FactItem(
                fact_id=f"fact:{evidence_id}",
                text=(
                    f"能力运行 #{run.id} 的状态为 {run.status}"
                    f"{f'，原因：{run.error_message}' if run.error_message else ''}。"
                ),
                evidence_ids=[evidence_id],
                verification_status="derived",
            )
        )
    return [
        _tool("capability_readiness", "读取能力配置、前置条件和最近运行状态。", facts, evidence),
        _tool("capability_run_preview", "读取最近一次能力产物和输入快照。", facts, evidence),
        _tool("capability_run_explain", "解释能力运行状态与证据不足原因。", facts, evidence),
    ]


def _resource_agent_tools(db: Session) -> list[RegisteredAgentTool]:
    rows = list(
        db.execute(
            select(Source, SourceChannel)
            .join(SourceChannel, SourceChannel.source_id == Source.id)
            .where(SourceChannel.status.in_(("shadow", "active")))
            .order_by(Source.name, SourceChannel.id)
            .limit(20)
        )
    )
    evidence = [
        EvidenceItem(
            evidence_id=f"source-channel:{channel.id}",
            kind="source_catalog",
            title=source.name,
            source_name=source.organization_name or source.name,
            source_url=channel.entry_url,
            published_at=None,
            observed_at=_iso_datetime(channel.updated_at),
            retrieved_at=_iso_datetime(datetime.now(UTC)),
            evidence_locator={"source_id": source.id, "channel_id": channel.id},
            verification_status="derived",
            payload={"source_type": source.source_type, "status": channel.status},
        )
        for source, channel in rows
    ]
    facts = [
        FactItem(
            fact_id=f"fact:{item.evidence_id}",
            text=(
                f"信源《{item.title}》类型为 {item.payload['source_type']}，"
                f"接入状态为 {item.payload['status']}。"
            ),
            evidence_ids=[item.evidence_id],
            verification_status="derived",
        )
        for item in evidence
    ]
    return [
        _tool("source_catalog_search", "读取已登记的来源及正式入口。", facts, evidence),
        _tool("source_coverage_check", "检查已登记来源类型与接入状态。", facts, evidence),
    ]


def _system_agent_tools(db: Session) -> list[RegisteredAgentTool]:
    readiness = get_reader_bootstrap(db)
    evidence = EvidenceItem(
        evidence_id="calculation:reader-readiness",
        kind="calculation",
        title="Reader 实时启动诊断",
        source_name="国别智枢确定性检查",
        source_url=None,
        published_at=None,
        observed_at=_iso_datetime(datetime.now(UTC)),
        retrieved_at=_iso_datetime(datetime.now(UTC)),
        evidence_locator={"endpoint": "/api/v1/reader/bootstrap"},
        origin="calculation",
        verification_status="derived",
        payload=readiness,
    )
    fact = FactItem(
        fact_id="fact:calculation:reader-readiness",
        text=(
            f"Reader 当前有 {readiness['event_count']} 个 reviewed 事件、"
            f"{readiness['reviewed_material_count']} 份已确认材料、"
            f"{readiness['capability_runs_ready']} 个成功且非空的能力运行。"
        ),
        evidence_ids=[evidence.evidence_id],
        origin="calculation",
        verification_status="derived",
    )
    return [_tool("readiness_explain", "读取实时 readiness 与数据缺口。", [fact], [evidence])]


def _tool(
    name: str,
    description: str,
    facts: list[FactItem],
    evidence: list[EvidenceItem],
    *,
    calculations: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
) -> RegisteredAgentTool:
    return RegisteredAgentTool(
        name=name,
        description=description,
        result=ToolResult(
            capability=name,
            status="succeeded" if facts else "insufficient_data",
            summary=f"返回 {len(facts)} 条受控事实。" if facts else "当前没有达到门槛的事实。",
            evidence=evidence,
            facts=facts,
            calculations=calculations or [],
            limitations=limitations or ([] if facts else ["当前作用域证据不足。"]),
        ),
    )


def _assistant_related_questions(scope_type: str) -> list[str]:
    return {
        "country": ["当前宏观底账有哪些？", "贸易趋势有什么变化？", "哪些数据需要更新？"],
        "event": ["不同来源有哪些表述差异？", "当前事件还缺哪些证据？"],
        "topic": ["专题已确认材料有哪些？", "专题周报可以生成了吗？"],
        "capability": ["最近一次运行为什么失败？", "当前能力可以重新运行吗？"],
        "resource": ["当前已接入哪些官方来源？", "来源覆盖还缺什么？"],
        "system": ["当前 readiness 的主要缺口是什么？"],
    }[scope_type]


def _append_live_search(
    artifact: Any,
    *,
    settings: Any,
    question: str,
    scope_context: dict[str, Any],
    online_mode: str,
    progress: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    has_database_claims = any(section.claims for section in artifact.sections)
    wants_fresh = any(
        word in question.lower() for word in ("最新", "近期", "现在", "联网", "today", "latest")
    )
    should_search = online_mode == "on" or (
        online_mode == "auto" and (wants_fresh or not has_database_claims)
    )
    if not should_search:
        artifact.online_status = {
            "mode": online_mode,
            "provider": settings.live_search_provider,
            "state": "not_needed",
        }
        artifact.source_mode = "database"
        return
    if progress is not None:
        progress(
            "capability.started",
            {
                "capability": "live_search",
                "description": "检索联网即时信息，并与库内已复核证据分区。",
            },
        )
    provider = _live_search_provider(settings)
    if isinstance(provider, DisabledLiveSearchProvider):
        artifact.online_status = {
            "mode": online_mode,
            "provider": settings.live_search_provider,
            "state": "unavailable",
            "message": "联网暂不可用；已保留库内证据回答。",
        }
        artifact.limitations.append("联网暂不可用；即时信息未加入本次回答。")
        artifact.source_mode = "database"
        if progress is not None:
            progress(
                "capability.completed",
                {"capability": "live_search", "status": "unavailable", "finding_count": 0},
            )
        return
    try:
        bundle = provider.search(question=question, scope_context=scope_context)
    except AgentRuntimeError as exc:
        artifact.online_status = {
            "mode": online_mode,
            "provider": provider.provider_name,
            "state": "failed",
            "message": str(exc),
        }
        artifact.limitations.append("联网检索失败；已保留库内证据回答。")
        artifact.source_mode = "database"
        if progress is not None:
            progress(
                "capability.completed",
                {"capability": "live_search", "status": "failed", "finding_count": 0},
            )
        return
    live_claims: list[AnswerClaim] = []
    for index, finding in enumerate(bundle.findings, start=1):
        evidence_id = f"live-web:{index}:{hashlib.sha256(finding.source_url.encode()).hexdigest()[:12]}"
        artifact.citations.append(
            EvidenceItem(
                evidence_id=evidence_id,
                kind="live_web",
                title=finding.title,
                source_name=finding.source_name,
                source_url=finding.source_url,
                published_at=finding.published_at,
                observed_at=_iso_datetime(datetime.now(UTC)),
                retrieved_at=_iso_datetime(datetime.now(UTC)),
                published_at_precision=finding.published_at_precision,
                evidence_locator=finding.evidence_locator.model_dump(mode="json", exclude_none=True),
                review_status="live_unreviewed",
                origin="live_web",
                verification_status="live_unreviewed",
                payload={},
            )
        )
        live_claims.append(
            AnswerClaim(
                text=finding.claim,
                fact_ids=[f"live-fact:{evidence_id}"],
                evidence_ids=[evidence_id],
                origin="live_web",
                verification_status="live_unreviewed",
            )
        )
    if live_claims:
        artifact.sections.append(
            AnswerSection(title="联网补充信息（未入库复核）", claims=live_claims, origin="live_web")
        )
    artifact.limitations.extend(bundle.limitations)
    artifact.online_status = {
        "mode": online_mode,
        "provider": provider.provider_name,
        "state": "succeeded" if live_claims else "no_results",
        "retrieved_at": _iso_datetime(datetime.now(UTC)),
    }
    if has_database_claims and live_claims:
        artifact.source_mode = "hybrid"
    else:
        artifact.source_mode = "live_web" if live_claims else "database"
    if progress is not None:
        progress(
            "capability.completed",
            {
                "capability": "live_search",
                "status": artifact.online_status["state"],
                "finding_count": len(live_claims),
            },
        )


def _live_search_provider(settings: Any) -> LiveSearchProvider:
    if settings.live_search_provider in {"disabled", "coze_test"}:
        return DisabledLiveSearchProvider()
    if settings.live_search_provider == "codex_local":
        if settings.environment == "production":
            raise AgentRuntimeError("local Codex live search is disabled in production")
        return CodexLocalLiveSearchProvider(
            binary=settings.agent_codex_binary,
            model=settings.live_search_model or settings.agent_model,
            timeout_seconds=settings.live_search_timeout_seconds,
        )
    if settings.live_search_provider == "openai_responses":
        if settings.agent_openai_api_key is None:
            return DisabledLiveSearchProvider()
        return OpenAIResponsesLiveSearchProvider(
            api_key=settings.agent_openai_api_key,
            model=settings.live_search_model or settings.agent_model,
            timeout_seconds=settings.live_search_timeout_seconds,
        )
    return DisabledLiveSearchProvider()


def _proposed_assistant_actions(
    db: Session,
    *,
    context: AssistantContextPayload,
    run_id: int,
    question: str,
) -> list[dict[str, Any]]:
    if context.space == "country" and context.country_iso3 is None:
        return []
    actions: list[dict[str, Any]] = []
    topic_intent = any(
        keyword in question
        for keyword in ("专题", "新建", "创建", "放到", "加入", "整理", "跟踪", "研究空间")
    )
    if (context.space == "event" and context.event_id is not None) or (
        context.space in {"country", "event", "topic"} and topic_intent
    ):
        actions.append(
            {
                "action_id": f"prepare-topic-draft-{run_id}",
                "type": "topic_draft_prepare",
                "label": "加入已有专题或整理为新专题",
                "confirmation_required": True,
                "status": "proposed",
                "assistant_run_id": run_id,
            }
        )
    slug = {
        "country": "country-brief",
        "event": "event-timeline",
        "topic": "topic-digest",
        "capability": None,
    }.get(context.space)
    configs: list[CapabilityConfig] = []
    if context.capability_config_ids:
        configs = [
            config
            for config_id in context.capability_config_ids
            if (config := db.get(CapabilityConfig, config_id)) is not None
        ]
    elif context.capability_run_id:
        capability_run = db.get(CapabilityRun, context.capability_run_id)
        config = db.get(CapabilityConfig, capability_run.config_id) if capability_run else None
        configs = [config] if config is not None else []
    elif slug:
        config = db.scalar(
            select(CapabilityConfig)
            .join(CapabilityTemplate, CapabilityTemplate.id == CapabilityConfig.template_id)
            .where(CapabilityConfig.status == "active", CapabilityTemplate.slug == slug)
            .order_by(CapabilityConfig.id)
            .limit(1)
        )
        configs = [config] if config is not None else []
    if not configs:
        return actions
    for position, config in enumerate(configs, start=1):
        if context.space == "country" and context.country_iso3:
            configured_iso3 = str(config.config.get("country_iso3") or "").upper()
            configured_iso3s = {
                str(item).upper() for item in (config.config.get("country_iso3s") or []) if item
            }
            if configured_iso3 and configured_iso3 != context.country_iso3.upper():
                continue
            if configured_iso3s and context.country_iso3.upper() not in configured_iso3s:
                continue
        research_case_id = context.research_case_id or config.config.get("research_case_id")
        if context.space == "event" and context.event_id:
            research_case_id = (
                db.scalar(
                    select(ResearchCaseEvent.research_case_id)
                    .where(ResearchCaseEvent.event_id == context.event_id)
                    .order_by(ResearchCaseEvent.research_case_id)
                    .limit(1)
                )
                or research_case_id
            )
        actions.append(
            {
                "action_id": f"run-capability-{config.id}",
                "type": "capability_run_execute",
                "label": (
                    "生成专题日报／周报"
                    if slug == "topic-digest" and len(configs) == 1
                    else f"{position}. 运行《{config.name}》"
                ),
                "config_id": config.id,
                "research_case_id": research_case_id,
                "confirmation_required": True,
                "status": "proposed",
                "assistant_run_id": run_id,
                "sequence": position,
            }
        )
    return actions


def _assistant_response(agent_run: AgentRun, artifact: Any) -> dict[str, Any]:
    citations = [
        {
            "type": item.kind,
            "title": item.title,
            "source_name": item.source_name,
            "url": item.source_url,
            "evidence_id": item.evidence_id,
            "published_at": item.published_at,
            "observed_at": item.observed_at,
            "retrieved_at": item.retrieved_at,
            "published_at_precision": item.published_at_precision,
            "evidence_locator": item.evidence_locator,
            "review_status": item.review_status,
            "origin": item.origin,
            "verification_status": item.verification_status,
        }
        for item in artifact.citations
    ]
    return {
        "conversation_id": agent_run.conversation_id,
        "scope_type": agent_run.scope_type,
        "scope_key": agent_run.scope_key,
        "question": agent_run.question,
        "run_id": agent_run.id,
        "workflow": artifact.workflow,
        "plan": agent_run.plan,
        "runtime": artifact.runtime,
        "status": artifact.status,
        "source_mode": artifact.source_mode,
        "online_status": artifact.online_status,
        "answer": artifact_markdown(artifact),
        "citations": citations,
        "related_questions": artifact.related_questions,
        "proposed_actions": artifact.proposed_actions,
        "artifact": artifact.model_dump(mode="json"),
    }


def artifact_markdown_from_dict(artifact: dict[str, Any]) -> str:
    blocks = []
    for section in artifact.get("sections", []):
        claims = "\n".join(
            f"- {claim.get('text')}" for claim in section.get("claims", []) if claim.get("text")
        )
        if claims:
            blocks.append(f"### {section.get('title') or '受控证据'}\n\n{claims}")
    return "\n\n".join(blocks) or "### 证据不足\n\n当前问题没有可用的已复核事实。"


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat()


def _published_at_precision(version: DocumentVersion) -> str:
    metadata = version.source_metadata or {}
    provenance = metadata.get("published_at_provenance")
    if isinstance(provenance, dict) and provenance.get("precision") in {
        "day",
        "month",
        "year",
        "unknown",
    }:
        return provenance["precision"]
    return "unknown"


def _display_published_at(value: str | None, precision: str) -> str:
    if not value or precision == "unknown":
        return "来源未提供发布日期"
    if precision == "year":
        return f" {value[:4]} 年（年份精度）"
    if precision == "month":
        return f" {value[:7]}（月份精度）"
    return f" {value[:10]}"


def _event_source_count(db: Session, event_id: int) -> int:
    return (
        db.scalar(
            select(func.count(func.distinct(Document.source_id)))
            .select_from(EventMention)
            .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(EventMention.event_id == event_id, EventMention.review_status == "confirmed")
        )
        or 0
    )


def _latest_datetime(values: list[datetime | None]) -> datetime | None:
    normalized = [
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        for value in values
        if value is not None
    ]
    return max(normalized, default=None)


def require_reader_write_access() -> None:
    if get_settings().public_api_read_only:
        raise HTTPException(status_code=403, detail="public API is read-only")


def require_internal_reader_access() -> None:
    if get_settings().public_api_read_only:
        raise HTTPException(status_code=403, detail="private field materials are unavailable")


def _active_research_case(db: Session, case_id: int) -> ResearchCase:
    research_case = db.get(ResearchCase, case_id)
    if research_case is None:
        raise HTTPException(status_code=404, detail="research case not found")
    if research_case.status != "active":
        raise HTTPException(status_code=409, detail="archived research case cannot be changed")
    return research_case


def _material_is_confirmed(db: Session, document_version_id: int) -> bool:
    return bool(
        db.scalar(
            select(
                or_(
                    select(DocumentEntity.document_version_id)
                    .where(
                        DocumentEntity.document_version_id == document_version_id,
                        DocumentEntity.review_status == "confirmed",
                    )
                    .exists(),
                    select(EventMention.document_version_id)
                    .where(
                        EventMention.document_version_id == document_version_id,
                        EventMention.review_status == "confirmed",
                    )
                    .exists(),
                )
            )
        )
    )


class ResearchCaseCreatePayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    research_question: str = Field(default="", max_length=1000)
    country_iso3: str = Field(default="COD", pattern=r"^[A-Z]{3}$")
    brief_original: str = Field(default="", max_length=6000)
    brief_confirmed: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)
    candidate_document_version_ids: list[int] = Field(default_factory=list, max_length=30)
    selected_document_version_ids: list[int] = Field(default_factory=list, max_length=30)


class ResearchCaseSuggestionPayload(BaseModel):
    country_iso3: str = Field(default="COD", pattern=r"^[A-Z]{3}$")
    research_intent: str | None = Field(default=None, min_length=4, max_length=1000)
    seed_event_id: int | None = Field(default=None, gt=0)
    seed_document_version_id: int | None = Field(default=None, gt=0)
    limit: int = Field(default=4, ge=1, le=5)


class ResearchCaseFromSuggestionPayload(BaseModel):
    suggestion_id: str = Field(min_length=8, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    research_question: str = Field(min_length=1, max_length=1000)
    country_iso3: str = Field(default="COD", pattern=r"^[A-Z]{3}$")
    research_intent: str | None = Field(default=None, min_length=4, max_length=1000)
    source_run_id: int | None = Field(default=None, gt=0)
    event_ids: list[int] = Field(default_factory=list, max_length=50)
    document_version_ids: list[int] = Field(default_factory=list, max_length=100)


class AssistantTopicDraftPayload(BaseModel):
    research_intent: str | None = Field(default=None, min_length=4, max_length=1000)
    current_title: str | None = Field(default=None, min_length=1, max_length=160)
    current_research_question: str | None = Field(default=None, min_length=1, max_length=1000)
    revision_instruction: str | None = Field(default=None, min_length=2, max_length=500)


class ResearchCaseEventPayload(BaseModel):
    event_id: int = Field(gt=0)
    usage_type: str = Field(default="primary", pattern=r"^(primary|background|monitoring)$")


class ResearchCaseMaterialPayload(BaseModel):
    document_version_id: int = Field(gt=0)
    usage_type: str = Field(default="background", pattern=r"^(support|background|refute|to_verify)$")


class ResearchCaseDataSlicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: int = Field(gt=0)
    snapshot_id: int | None = Field(default=None, gt=0)
    label: str = Field(min_length=1, max_length=240)
    filter_spec: dict[str, Any]


class FieldMaterialCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=160)
    filename: str = Field(min_length=1, max_length=240)
    content_type: str = Field(default="", max_length=120)
    content_base64: str = Field(min_length=4, max_length=35_000_000)
    material_type: str = Field(
        default="field_note",
        pattern=r"^(field_note|interview_transcript|photo|supporting_document)$",
    )
    privacy_level: str = Field(
        default="restricted",
        pattern=r"^(restricted|anonymized|shareable)$",
    )
    country_iso3: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    research_case_id: int | None = Field(default=None, gt=0)
    captured_on: date | None = None
    method_note: str = Field(default="", max_length=2000)
    authorization_confirmed: bool = False

    @model_validator(mode="after")
    def validate_scope_and_authorization(self) -> FieldMaterialCreatePayload:
        if not self.authorization_confirmed:
            raise ValueError("authorization_confirmed must be true")
        if not self.country_iso3 and not self.research_case_id:
            raise ValueError("请选择国家或项目")
        return self


def _reader_demo_owner(db: Session) -> User:
    owner = db.scalar(select(User).where(User.email == "mvp-demo@local.invalid", User.status == "active"))
    if owner is None:
        owner = User(
            email="mvp-demo@local.invalid",
            display_name="国别智枢 MVP Demo",
            role="user",
            status="active",
        )
        db.add(owner)
        db.flush()
    return owner


def _reader_identity_initialized(db):
    from app.services.community_auth import accounts_initialized

    return reader_keys_initialized(db) or accounts_initialized(db)


def _current_reader_actor(
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
    request: Request = None,
) -> User:
    from app.services.community_auth import CURRENT_REQUEST, accounts_initialized, authenticate_session

    if accounts_initialized(db):
        request = request or CURRENT_REQUEST.get()
        if request is None:
            raise HTTPException(401, "请先登录平台账号")
        return authenticate_session(db, request, required=True)
    if not _reader_identity_initialized(db):
        return _reader_demo_owner(db)
    user = authenticate_reader_key(db, x_reader_key)
    if user is None:
        raise HTTPException(status_code=401, detail="请使用本机成员访问密钥")
    db.commit()
    return user


ReaderActor = Annotated[User, Depends(_current_reader_actor)]


def _optional_reader_actor(
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
    request: Request = None,
) -> User | None:
    from app.services.community_auth import CURRENT_REQUEST, accounts_initialized, authenticate_session

    if accounts_initialized(db):
        request = request or CURRENT_REQUEST.get()
        return authenticate_session(db, request) if request else None
    if not _reader_identity_initialized(db):
        return _reader_demo_owner(db)
    user = authenticate_reader_key(db, x_reader_key)
    if user is not None:
        db.commit()
    return user


OptionalReaderActor = Annotated[User | None, Depends(_optional_reader_actor)]


def _case_permission(db: Session, case_id: int, actor: User, permission: str) -> ResearchCaseMember:
    try:
        return require_case_permission(db, case_id, actor, permission)
    except ReaderAccessDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


class ReaderIdentityBootstrapPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="负责人本机密钥", min_length=2, max_length=120)


class ResearchMemberInvitePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern=r"^(reviewer|editor|viewer)$")
    key_label: str = Field(default="专题成员密钥", min_length=2, max_length=120)


class ResearchMemberRolePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^(reviewer|editor|viewer)$")


@router.get("/auth/status")
def get_reader_identity_status(db: DbSession, request: Request) -> dict[str, Any]:
    return {
        "initialized": _reader_identity_initialized(db),
        "bootstrap_allowed": bool(request.client and request.client.host in {"127.0.0.1", "::1"}),
        "header": "X-Reader-Key",
        "storage_note": "原始密钥只在创建时返回，服务端只保存 SHA-256。",
    }


@router.post("/auth/bootstrap", status_code=status.HTTP_201_CREATED)
def bootstrap_reader_identity(
    payload: ReaderIdentityBootstrapPayload,
    request: Request,
    db: DbSession,
) -> dict[str, Any]:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="负责人身份只能从本机回环地址初始化")
    if _reader_identity_initialized(db):
        raise HTTPException(status_code=409, detail="负责人身份已初始化")
    owner = _reader_demo_owner(db)
    ensure_owner_memberships(db, owner)
    legacy_assistant_runs_claimed = db.execute(
        update(AgentRun).where(AgentRun.requested_by.is_(None)).values(requested_by=owner.id)
    ).rowcount
    issued = issue_reader_key(db, owner, label=payload.label)
    db.commit()
    return {
        "user_id": owner.id,
        "display_name": owner.display_name,
        "role": "owner",
        "access_key": issued.raw_key,
        "key_prefix": issued.record.key_prefix,
        "show_once": True,
        "legacy_assistant_runs_claimed": legacy_assistant_runs_claimed,
    }


@router.get("/research-cases/{case_id}/collaboration")
def get_research_case_collaboration(
    case_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    current = _case_permission(db, case_id, actor, "view")
    rows = list(
        db.execute(
            select(ResearchCaseMember, User)
            .join(User, User.id == ResearchCaseMember.user_id)
            .where(ResearchCaseMember.research_case_id == case_id)
            .order_by(ResearchCaseMember.id)
        )
    )
    contributions = list(
        db.execute(
            select(ResearchContribution, User)
            .outerjoin(User, User.id == ResearchContribution.user_id)
            .where(ResearchContribution.research_case_id == case_id)
            .order_by(ResearchContribution.created_at.desc(), ResearchContribution.id.desc())
            .limit(100)
        )
    )
    member_payloads = [public_member_payload(member, user) for member, user in rows]
    return {
        "current_member": public_member_payload(current, actor),
        "members": member_payloads,
        "permission_matrix": {role: sorted(permissions) for role, permissions in ROLE_PERMISSIONS.items()},
        "public_publishing": {
            "enabled": False,
            "note": "具备发布审批权限，但平台尚未开放公网发布。",
        },
        "contributions": [
            {
                "id": item.id,
                "action_type": item.action_type,
                "object_type": item.object_type,
                "object_key": item.object_key,
                "details": (
                    {"restricted": True}
                    if item.action_type == "workflow_metadata_revised"
                    and "view_restricted" not in ROLE_PERMISSIONS[current.role]
                    else item.details
                ),
                "actor": user.display_name if user else "系统处理",
                "created_at": item.created_at,
            }
            for item, user in contributions
        ],
    }


@router.post(
    "/research-cases/{case_id}/members",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def invite_research_case_member(
    case_id: int,
    payload: ResearchMemberInvitePayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "manage_members")
    email = payload.email.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    from app.models.field_community import ReaderAccount
    from app.services.community_auth import accounts_initialized

    multiuser = accounts_initialized(db)
    if multiuser and (
        not user or not db.get(ReaderAccount, user.id) or not db.get(ReaderAccount, user.id).password_hash
    ):
        raise HTTPException(422, "请先由管理员邀请该成员建立平台账号")
    if user is None:
        user = User(email=email, display_name=payload.display_name.strip(), role="user", status="active")
        db.add(user)
        db.flush()
    existing = db.scalar(
        select(ResearchCaseMember).where(
            ResearchCaseMember.research_case_id == case_id,
            ResearchCaseMember.user_id == user.id,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该用户已是专题成员")
    member = ResearchCaseMember(
        research_case_id=case_id,
        user_id=user.id,
        role=payload.role,
        added_by=actor.id,
    )
    db.add(member)
    db.flush()
    issued = None if multiuser else issue_reader_key(db, user, label=payload.key_label)
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="member_invited",
        object_type="research_case_member",
        object_key=member.id,
        details={"role": member.role, "user_id": user.id},
    )
    db.commit()
    return {
        "member": public_member_payload(member, user),
        "access_key": issued.raw_key if issued else None,
        "key_prefix": issued.record.key_prefix if issued else None,
        "show_once": not multiuser,
    }


@router.patch(
    "/research-cases/{case_id}/members/{member_id}",
    dependencies=[Depends(require_reader_write_access)],
)
def update_research_case_member(
    case_id: int,
    member_id: int,
    payload: ResearchMemberRolePayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "manage_members")
    member = db.get(ResearchCaseMember, member_id)
    if member is None or member.research_case_id != case_id:
        raise HTTPException(status_code=404, detail="专题成员不存在")
    if member.role == "owner":
        raise HTTPException(status_code=422, detail="不能通过此接口更改负责人角色")
    member.role = payload.role
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="member_role_changed",
        object_type="research_case_member",
        object_key=member.id,
        details={"role": member.role},
    )
    db.commit()
    user = db.get(User, member.user_id)
    return public_member_payload(member, user)


@router.delete(
    "/research-cases/{case_id}/members/{member_id}",
    dependencies=[Depends(require_reader_write_access)],
)
def remove_research_case_member(
    case_id: int,
    member_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "manage_members")
    member = db.get(ResearchCaseMember, member_id)
    if member is None or member.research_case_id != case_id:
        raise HTTPException(status_code=404, detail="专题成员不存在")
    if member.role == "owner":
        raise HTTPException(status_code=422, detail="不能移除专题负责人")
    user_id = member.user_id
    db.delete(member)
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="member_removed",
        object_type="user",
        object_key=user_id,
    )
    db.commit()
    return {"removed": True, "user_id": user_id, "global_keys_unchanged": True}


@router.delete(
    "/research-cases/{case_id}/members/{member_id}/keys/{key_id}",
    dependencies=[Depends(require_reader_write_access)],
)
def revoke_research_case_member_key(
    case_id: int,
    member_id: int,
    key_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "manage_members")
    raise HTTPException(
        status_code=409,
        detail="项目权限不能吊销全局访问密钥；请仅移除项目成员授权",
    )


_TOPIC_INTENT_STOP_TERMS = {
    "一个",
    "什么",
    "关于",
    "分析",
    "如何",
    "专题",
    "当前",
    "影响",
    "想要",
    "我想",
    "持续",
    "相关",
    "研究",
    "跟踪",
    "进行",
}


def _topic_intent_tokens(value: str | None) -> set[str]:
    if not value:
        return set()
    normalized = value.lower().strip()
    tokens = set(re.findall(r"[a-z][a-z0-9.-]{1,}|\d{2,}", normalized))
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        cleaned = segment
        for stop_term in _TOPIC_INTENT_STOP_TERMS:
            cleaned = cleaned.replace(stop_term, " ")
        for part in cleaned.split():
            if len(part) <= 4:
                tokens.add(part)
                continue
            for size in (2, 3, 4):
                tokens.update(part[index : index + size] for index in range(len(part) - size + 1))
    return {token for token in tokens if token not in _TOPIC_INTENT_STOP_TERMS}


def _topic_text_match_score(tokens: set[str], *values: Any) -> int:
    if not tokens:
        return 0
    text_value = " ".join(str(value or "").lower() for value in values)
    return sum(min(len(token), 4) for token in tokens if token in text_value)


def _topic_intent_label(value: str) -> str:
    label = value.strip(" ，。！？,.!?")
    for prefix in ("我想研究", "我想了解", "帮我研究", "请研究", "围绕", "关于"):
        if label.startswith(prefix):
            label = label[len(prefix) :].strip(" ，。！？,.!?")
    return label[:64] or "新研究专题"


def _topic_suggestion_rows(
    db: Session,
    payload: ResearchCaseSuggestionPayload,
) -> list[dict[str, Any]]:
    country = db.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == payload.country_iso3,
        )
    )
    if country is None:
        return []
    events = list(
        db.scalars(
            select(ResearchEvent)
            .where(
                ResearchEvent.country_entity_id == country.id,
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(
                ResearchEvent.start_at.is_(None),
                ResearchEvent.start_at.desc(),
                ResearchEvent.id.desc(),
            )
            .limit(40)
        )
    )
    event_ids = [event.id for event in events]
    entity_rows = list(
        db.execute(
            select(EventEntity, ResearchEntity)
            .join(ResearchEntity, ResearchEntity.id == EventEntity.entity_id)
            .where(EventEntity.event_id.in_(event_ids))
            .order_by(EventEntity.event_id, EventEntity.role, ResearchEntity.canonical_name)
        )
    )
    entities_by_event: dict[int, list[tuple[EventEntity, ResearchEntity]]] = defaultdict(list)
    for link, entity in entity_rows:
        entities_by_event[link.event_id].append((link, entity))
    mention_rows = list(
        db.scalars(
            select(EventMention)
            .where(
                EventMention.event_id.in_(event_ids),
                EventMention.review_status == "confirmed",
            )
            .order_by(EventMention.event_id, EventMention.document_version_id)
        )
    )
    versions_by_event: dict[int, list[int]] = defaultdict(list)
    for mention in mention_rows:
        versions_by_event[mention.event_id].append(mention.document_version_id)

    country_material_rows = list(
        db.execute(
            select(DocumentVersion, Document)
            .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentEntity.entity_id == country.id,
                DocumentEntity.review_status == "confirmed",
            )
            .distinct()
            .order_by(Document.published_at.desc(), DocumentVersion.id.desc())
            .limit(80)
        )
    )
    material_by_version = {
        version.id: {
            "document_version_id": version.id,
            "document_id": document.id,
            "title": version.title or document.title,
            "published_at": version.published_at or document.published_at,
            "source_space": "country",
            "search_text": " ".join([version.title or document.title, version.abstract or ""]),
        }
        for version, document in country_material_rows
    }
    event_material_ids = {
        version_id for version_ids in versions_by_event.values() for version_id in version_ids
    }
    missing_event_material_ids = event_material_ids - set(material_by_version)
    if missing_event_material_ids:
        for version, document in db.execute(
            select(DocumentVersion, Document)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.id.in_(missing_event_material_ids))
        ):
            material_by_version[version.id] = {
                "document_version_id": version.id,
                "document_id": document.id,
                "title": version.title or document.title,
                "published_at": version.published_at or document.published_at,
                "source_space": "event",
                "search_text": " ".join([version.title or document.title, version.abstract or ""]),
            }

    def deduplicate_material_versions(version_ids: list[int]) -> list[int]:
        selected_by_document: dict[int, int] = {}
        for version_id in version_ids:
            material = material_by_version.get(version_id)
            document_id = int(material["document_id"]) if material else -version_id
            selected_by_document[document_id] = max(
                version_id,
                selected_by_document.get(document_id, version_id),
            )
        return list(selected_by_document.values())

    research_intent = (payload.research_intent or "").strip()
    intent_tokens = _topic_intent_tokens(research_intent)
    matching_country_material_ids = deduplicate_material_versions(
        [
            version_id
            for version_id, material in material_by_version.items()
            if material["source_space"] == "country"
            and _topic_text_match_score(intent_tokens, material["search_text"]) > 0
        ]
    )

    groups: dict[str, dict[str, Any]] = {}
    type_labels = {
        "policy": "政策变化与治理",
        "conflict": "安全事件与矿区治理",
        "market": "关键矿产市场变化",
        "accident": "生产安全与监管",
        "other": "近期重点动态",
    }
    for event in events:
        entity_pairs = entities_by_event[event.id]
        focus_entities = [
            entity
            for link, entity in entity_pairs
            if link.role in {"commodity", "policy", "location", "actor"}
        ]
        primary_entity = focus_entities[0] if focus_entities else None
        if event.series_key:
            group_key = f"series:{event.series_key}"
            basis = "同一事件系列"
        elif primary_entity is not None:
            group_key = f"entity:{primary_entity.id}:{event.event_type}"
            basis = f"共同涉及{primary_entity.canonical_name}"
        else:
            group_key = f"type:{event.event_type}"
            basis = f"同属{type_labels.get(event.event_type, '近期动态')}"
        group = groups.setdefault(
            group_key,
            {
                "key": group_key,
                "basis": basis,
                "events": [],
                "entities": [],
                "document_version_ids": [],
            },
        )
        group["events"].append(event)
        group["entities"].extend(entity.canonical_name for entity in focus_entities[:3])
        group["document_version_ids"].extend(versions_by_event[event.id])

    event_group_intent_matches = []
    for group in groups.values():
        event_group_intent_matches.append(
            _topic_text_match_score(
                intent_tokens,
                group["basis"],
                *group["entities"],
                *(event.title for event in group["events"]),
                *(event.summary for event in group["events"]),
                *(type_labels.get(event.event_type, event.event_type) for event in group["events"]),
            )
        )
    if research_intent and matching_country_material_ids and not any(event_group_intent_matches):
        groups["country-materials:intent"] = {
            "key": "country-materials:intent",
            "basis": "国别空间确认材料与研究意图匹配",
            "events": [],
            "entities": [],
            "document_version_ids": matching_country_material_ids,
        }

    suggestions: list[dict[str, Any]] = []
    for group in groups.values():
        group_events = group["events"]
        entity_names = list(dict.fromkeys(group["entities"]))
        intent_match_rank = _topic_text_match_score(
            intent_tokens,
            group["basis"],
            *entity_names,
            *(event.title for event in group_events),
            *(event.summary for event in group_events),
            *(type_labels.get(event.event_type, event.event_type) for event in group_events),
        )
        if group_events:
            focus = (
                entity_names[0] if entity_names else type_labels.get(group_events[0].event_type, "重点议题")
            )
            title = f"{focus}持续跟踪"
        else:
            focus = _topic_intent_label(research_intent)
            title = focus
        if group_events and group["key"].startswith("series:"):
            title = f"{group_events[0].title}系列跟踪"
        question = f"{focus}相关事件、政策变化与不同来源的证据口径如何演变？"
        if research_intent:
            question = f"围绕“{research_intent}”，现有事件、政策与不同来源的证据如何相互印证？"
        document_version_ids = list(dict.fromkeys(group["document_version_ids"]))
        if research_intent and intent_match_rank > 0:
            document_version_ids.extend(
                version_id
                for version_id in matching_country_material_ids
                if version_id not in document_version_ids
            )
        document_version_ids = deduplicate_material_versions(list(dict.fromkeys(document_version_ids)))
        material_intent_rank = max(
            (
                _topic_text_match_score(
                    intent_tokens,
                    material_by_version[version_id]["search_text"],
                )
                for version_id in document_version_ids
                if version_id in material_by_version
            ),
            default=0,
        )
        intent_match_rank = max(intent_match_rank, material_intent_rank)
        confidence = (
            0.9
            if group["key"].startswith("series:")
            else 0.78
            if entity_names
            else 0.72
            if document_version_ids
            else 0.62
        )
        gaps = []
        if len(group_events) < 2:
            gaps.append(
                "当前尚未关联已复核事件，后续需从事件空间补充时间线。"
                if not group_events
                else "当前只有一条已复核事件，后续需持续补充时间序列。"
            )
        if len(document_version_ids) < 2:
            gaps.append("当前确认材料较少，暂不适合直接形成正式周报。")
        signature = ":".join(str(event.id) for event in group_events) or ":".join(
            str(version_id) for version_id in document_version_ids
        )
        suggestion_id = hashlib.sha256(
            f"{payload.country_iso3}:{group['key']}:{research_intent}:{signature}".encode()
        ).hexdigest()[:20]
        materials = []
        for version_id in document_version_ids:
            if version_id not in material_by_version:
                continue
            material = {
                key: value for key, value in material_by_version[version_id].items() if key != "search_text"
            }
            if version_id in event_material_ids and material["source_space"] == "country":
                material["source_space"] = "country_event"
            materials.append(material)
        event_material_count = len(event_material_ids.intersection(document_version_ids))
        suggestions.append(
            {
                "suggestion_id": suggestion_id,
                "title": title[:160],
                "research_question": question,
                "event_ids": [event.id for event in group_events],
                "events": [
                    {
                        "id": event.id,
                        "title": event.title,
                        "start_at": event.start_at,
                        "event_type": event.event_type,
                    }
                    for event in group_events
                ],
                "document_version_ids": document_version_ids,
                "materials": materials,
                "reasons": [
                    group["basis"],
                    f"包含 {len(group_events)} 条已复核事件和 {len(document_version_ids)} 份确认材料",
                ],
                "confidence": confidence,
                "review_band": "recommended" if confidence >= 0.75 else "explore",
                "evidence_gaps": gaps,
                "seed_match": payload.seed_event_id in {event.id for event in group_events}
                or payload.seed_document_version_id in set(document_version_ids),
                "intent_match_score": min(
                    1.0,
                    intent_match_rank / max(1, len(intent_tokens) * 2),
                ),
                "source_summary": {
                    "reviewed_events": len(group_events),
                    "event_materials": event_material_count,
                    "country_materials": len(document_version_ids) - event_material_count,
                },
                "_intent_match_rank": intent_match_rank,
            }
        )
    if research_intent:
        matching_suggestions = [
            item for item in suggestions if item["_intent_match_rank"] > 0 or item["seed_match"]
        ]
        suggestions = matching_suggestions
    suggestions.sort(
        key=lambda item: (
            not item["seed_match"],
            -item["_intent_match_rank"],
            -len(item["event_ids"]),
            -len(item["document_version_ids"]),
            item["title"],
        )
    )
    for suggestion in suggestions:
        suggestion.pop("_intent_match_rank", None)
    return suggestions[: payload.limit]


@router.post("/research-cases/suggestions")
def suggest_research_cases(
    payload: ResearchCaseSuggestionPayload,
    db: DbSession,
) -> dict[str, Any]:
    _require_catalog_country(payload.country_iso3)
    return {
        "country_iso3": payload.country_iso3,
        "method": "reviewed-evidence-rules-v1",
        "research_intent": payload.research_intent,
        "formal_links_created": False,
        "suggestions": _topic_suggestion_rows(db, payload),
    }


@router.get("/project-frontiers")
@router.get("/topic-frontiers")
def list_topic_frontiers(
    db: DbSession,
    country_iso3: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "COD",
    q: Annotated[str | None, Query(min_length=1, max_length=360)] = None,
    limit: Annotated[int, Query(ge=1, le=12)] = 6,
) -> dict[str, Any]:
    _require_catalog_country(country_iso3)
    suggestions = _topic_suggestion_rows(
        db,
        ResearchCaseSuggestionPayload(
            country_iso3=country_iso3,
            research_intent=q,
            limit=min(limit, 5),
        ),
    )
    for item in suggestions:
        dated = [
            value
            for value in [
                *(event.get("start_at") for event in item.get("events", [])),
                *(material.get("published_at") for material in item.get("materials", [])),
            ]
            if value is not None
        ]
        item["latest_evidence_at"] = max(dated) if dated else None
        item["creation_requires_confirmation"] = True
    return {
        "country_iso3": country_iso3,
        "method": "reviewed-confirmed-evidence-only-v1",
        "formal_links_created": False,
        "candidates": suggestions,
        "limitations": [
            "候选只使用 reviewed 事件与 confirmed 材料；不会自动创建专题。",
            "来源不足时保留证据缺口，不用联网信息补成正式候选。",
        ],
    }


def _assistant_topic_focus(questions: list[str]) -> str | None:
    for question in reversed(questions):
        focus = question.strip()
        for fragment in (
            "麻烦你",
            "请帮我",
            "帮我",
            "我想要",
            "我想",
            "并结合我们刚才关注的",
            "结合我们刚才关注的",
            "我们刚才关注的",
            "结合前面讨论的",
            "把这个事件",
            "把当前事件",
            "将这个事件",
            "将当前事件",
            "放到专题空间里",
            "放到专题里",
            "加入专题空间",
            "加入专题",
            "整理成专题",
            "创建专题",
            "新建专题",
        ):
            focus = focus.replace(fragment, "")
        focus = focus.strip(" ，。！？,.!?里")
        for suffix in ("有哪些证据", "有什么证据", "如何变化", "整理一下", "整理"):
            if focus.endswith(suffix):
                focus = focus.removesuffix(suffix).strip(" ，。！？,.!?")
        if len(focus) >= 4:
            return focus[:100]
    return None


_TOPIC_DOMAIN_TERMS = (
    "矿产",
    "钴",
    "铜",
    "冲突",
    "安全",
    "武装",
    "政策",
    "治理",
    "市场",
    "贸易",
    "投资",
    "供应链",
    "监管",
    "生产",
)


def _topic_focus_score(suggestion: dict[str, Any], focus: str | None) -> int:
    if not focus:
        return 0
    haystack = "".join(
        [
            str(suggestion.get("title") or ""),
            str(suggestion.get("research_question") or ""),
            *(str(item.get("title") or "") for item in suggestion.get("events", [])),
        ]
    )
    return sum(1 for term in _TOPIC_DOMAIN_TERMS if term in focus and term in haystack)


def _revise_topic_draft(
    draft: dict[str, Any],
    payload: AssistantTopicDraftPayload,
) -> dict[str, Any]:
    instruction = (payload.revision_instruction or "").strip()
    if not instruction:
        return draft
    focus_terms = [term for term in _TOPIC_DOMAIN_TERMS if term in instruction]
    focus_label = "与".join(focus_terms[:3]) or _topic_intent_label(instruction)
    title = payload.current_title or str(draft["title"])
    question = payload.current_research_question or str(draft["research_question"])
    if "标题" in instruction or "聚焦" in instruction or "重点" in instruction:
        title = f"{focus_label}持续跟踪"
    if "短" in instruction:
        title = title[:28].rstrip("，、与和及")
    if any(keyword in instruction for keyword in ("问题", "聚焦", "重点", "强调", "加入", "增加", "改成")):
        question = f"围绕“{focus_label}”，现有事件、政策与不同来源的证据如何相互印证？"
    return {
        **draft,
        "title": title[:160],
        "research_question": question[:1000],
        "revision_instruction": instruction,
    }


@router.post("/assistant/runs/{run_id}/topic-draft")
def prepare_assistant_topic_draft(
    run_id: int,
    payload: AssistantTopicDraftPayload,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    agent_run = db.get(AgentRun, run_id)
    if agent_run is None or agent_run.status != "succeeded":
        raise HTTPException(status_code=404, detail="successful assistant run not found")
    actor = _require_assistant_run_access(db, agent_run, x_reader_key)
    context = dict(agent_run.scope_context or {})
    _require_assistant_case_access(
        db,
        _assistant_case_id(agent_run.scope_type, agent_run.scope_key, context),
        x_reader_key,
    )
    country_iso3 = str(context.get("country_iso3") or agent_run.country_iso3 or "COD").upper()
    seed_event_id = context.get("event_id") if agent_run.scope_type == "event" else None
    if seed_event_id is not None:
        event = db.get(ResearchEvent, int(seed_event_id))
        if event is None or event.review_status != "reviewed":
            raise HTTPException(status_code=422, detail="current event is unavailable or unreviewed")
    else:
        event = None
    conversation_statement = select(AgentRun).where(
        AgentRun.conversation_id == agent_run.conversation_id,
        AgentRun.scope_type == agent_run.scope_type,
        AgentRun.scope_key == agent_run.scope_key,
        AgentRun.status == "succeeded",
        AgentRun.id <= agent_run.id,
    )
    if (
        _assistant_case_id(
            agent_run.scope_type,
            agent_run.scope_key,
            agent_run.scope_context or {},
        )
        is None
    ):
        owner_filter = AgentRun.requested_by == actor.id
        if not _reader_identity_initialized(db):
            owner_filter = or_(owner_filter, AgentRun.requested_by.is_(None))
        conversation_statement = conversation_statement.where(owner_filter)
    conversation_runs = list(db.scalars(conversation_statement.order_by(AgentRun.id.desc()).limit(4)))
    questions = [item.question for item in reversed(conversation_runs)]
    focus = _assistant_topic_focus(questions)
    research_intent = (payload.research_intent or focus or "").strip()
    clustering_intent = " ".join(
        item for item in (research_intent, payload.revision_instruction or "") if item
    )[:1000]
    suggestions = _topic_suggestion_rows(
        db,
        ResearchCaseSuggestionPayload(
            country_iso3=country_iso3,
            research_intent=clustering_intent if len(clustering_intent) >= 4 else None,
            seed_event_id=int(seed_event_id) if seed_event_id is not None else None,
            limit=5,
        ),
    )
    if not suggestions:
        raise HTTPException(
            status_code=422,
            detail="当前没有足够的已复核事件来形成专题草稿，请先完成事件复核。",
        )
    suggestion = max(
        suggestions,
        key=lambda item: (
            bool(item.get("seed_match")),
            _topic_focus_score(item, focus),
            len(item.get("event_ids", [])),
            len(item.get("document_version_ids", [])),
        ),
    )
    draft = dict(suggestion)
    if focus and not payload.research_intent:
        if len(focus) <= 60:
            draft["title"] = f"{focus}持续跟踪"[:160]
        draft["research_question"] = (f"围绕对话关注的“{focus}”，{suggestion['research_question']}")[:1000]
        draft["reasons"] = [
            f"结合最近 {len(questions)} 轮对话中的研究关注点",
            *suggestion.get("reasons", []),
        ]
    draft = _revise_topic_draft(draft, payload)
    draft.update(
        {
            "source_run_id": agent_run.id,
            "conversation_id": agent_run.conversation_id,
            "conversation_turns_used": len(questions),
            "conversation_focus": focus,
            "research_intent": research_intent or None,
            "country_iso3": country_iso3,
            "seed_event_id": int(seed_event_id) if seed_event_id is not None else None,
            "seed_event_title": event.title if event is not None else None,
            "method": "assistant-conversation-plus-reviewed-evidence-rules-v1",
            "formal_links_created": False,
        }
    )
    return draft


@router.post(
    "/research-cases/from-suggestion",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def create_research_case_from_suggestion(
    payload: ResearchCaseFromSuggestionPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _require_catalog_country(payload.country_iso3)
    title = payload.title.strip()
    if db.scalar(
        select(ResearchCase.id).where(
            ResearchCase.owner_id == actor.id,
            func.lower(ResearchCase.title) == title.lower(),
            ResearchCase.scope["deleted_at"].as_string().is_(None),
        )
    ):
        raise HTTPException(status_code=409, detail="已有同名项目，请打开现有项目或修改标题")
    country = db.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == payload.country_iso3,
        )
    )
    events = list(db.scalars(select(ResearchEvent).where(ResearchEvent.id.in_(set(payload.event_ids)))))
    if len(events) != len(set(payload.event_ids)) or any(
        event.review_status != "reviewed" or country is None or event.country_entity_id != country.id
        for event in events
    ):
        raise HTTPException(status_code=422, detail="suggestion contains unavailable or unreviewed events")
    versions = list(
        db.scalars(select(DocumentVersion).where(DocumentVersion.id.in_(set(payload.document_version_ids))))
    )
    if len(versions) != len(set(payload.document_version_ids)) or any(
        not _material_is_confirmed(db, version.id) for version in versions
    ):
        raise HTTPException(status_code=422, detail="suggestion contains unconfirmed materials")
    research_case = ResearchCase(
        owner_id=actor.id,
        title=title,
        research_question=payload.research_question.strip(),
        scope={
            "country_iso3": payload.country_iso3,
            "scope_revision": 1,
            "context_bindings": {},
            "created_from_suggestion": payload.suggestion_id,
            "clustering_method": "reviewed-evidence-rules-v1",
            **({"research_intent": payload.research_intent.strip()} if payload.research_intent else {}),
            **({"assistant_source_run_id": payload.source_run_id} if payload.source_run_id else {}),
        },
        status="active",
    )
    db.add(research_case)
    db.flush()
    db.add_all(
        [
            ResearchCaseEvent(
                research_case_id=research_case.id,
                event_id=event.id,
                usage_type="primary",
            )
            for event in events
        ]
        + [
            ResearchCaseDocument(
                research_case_id=research_case.id,
                document_version_id=version.id,
                added_by=actor.id,
                usage_type="background",
                note="由专题候选聚类带入，已由用户确认。",
            )
            for version in versions
        ]
    )
    db.add(
        ResearchCaseMember(
            research_case_id=research_case.id,
            user_id=actor.id,
            role="owner",
            added_by=actor.id,
        )
    )
    record_contribution(
        db,
        research_case_id=research_case.id,
        user_id=actor.id,
        action_type="research_case_created_from_candidate",
        object_type="research_case",
        object_key=research_case.id,
        details={"suggestion_id": payload.suggestion_id},
    )
    db.commit()
    db.refresh(research_case)
    return {
        **_case_summary(db, research_case),
        "research_question": research_case.research_question,
        "scope": _public_case_scope(research_case.scope),
        "suggestion_id": payload.suggestion_id,
    }


@router.post(
    "/research-cases",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def create_research_case(
    payload: ResearchCaseCreatePayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _require_catalog_country(payload.country_iso3)
    if not payload.title.strip():
        raise HTTPException(422, "请填写项目标题")
    # Serialize owner creation to make retries and concurrent clicks idempotent.
    db.scalar(select(User).where(User.id == actor.id).with_for_update())
    request_hash = hashlib.sha256(
        json.dumps(
            payload.model_dump(exclude={"idempotency_key"}),
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    if payload.idempotency_key:
        prior_request = db.scalar(
            select(ResearchContribution).where(
                ResearchContribution.user_id == actor.id,
                ResearchContribution.action_type == "research_case_created",
                ResearchContribution.details["creation_request"].as_string() == payload.idempotency_key,
            )
        )
        if prior_request:
            if prior_request.details.get("creation_fingerprint") != request_hash:
                raise HTTPException(409, "同一建项请求标识不能用于不同内容")
            prior = db.get(ResearchCase, prior_request.research_case_id)
            _case_permission(db, prior.id, actor, "view")
            return {
                **_case_summary(db, prior),
                "research_question": prior.research_question,
                "scope": _public_case_scope(prior.scope),
            }
    valid_ids = []
    candidate_ids = set(payload.candidate_document_version_ids)
    selected_ids = set(payload.selected_document_version_ids)
    for version_id in sorted(candidate_ids | selected_ids):
        try:
            _validated_country_references(
                db,
                AssistantQuestionPayload(
                    question="校验建项候选材料",
                    context={"space": "country", "country_iso3": payload.country_iso3},
                    references=[{"type": "document_version", "id": version_id}],
                ),
            )
            if version_id in selected_ids and not _material_is_confirmed(db, version_id):
                raise HTTPException(422, "所选材料尚未复核，不能采用")
        except HTTPException:
            if version_id in selected_ids:
                raise
            continue
        valid_ids.append(version_id)
    title = payload.title.strip()
    research_question = payload.research_question.strip()
    existing = db.scalar(
        select(ResearchCase).where(
            ResearchCase.owner_id == actor.id,
            func.lower(ResearchCase.title) == title.lower(),
            ResearchCase.scope["deleted_at"].as_string().is_(None),
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="已有同名项目，请打开现有项目或修改标题")
    research_case = ResearchCase(
        owner_id=actor.id,
        title=title,
        research_question=research_question,
        brief_original=payload.brief_original or research_question,
        brief_confirmed=payload.brief_confirmed or {"purpose": research_question},
        scope={"country_iso3": payload.country_iso3, "scope_revision": 1, "context_bindings": {}},
        status="active",
    )
    db.add(research_case)
    db.flush()
    db.add(
        ResearchCaseMember(
            research_case_id=research_case.id,
            user_id=actor.id,
            role="owner",
            added_by=actor.id,
        )
    )
    from app.api.project_workspace import _material_snapshot

    for version_id in valid_ids:
        candidate = project_workflow.add_candidate(
            db,
            research_case.id,
            None,
            "document_version",
            version_id,
            _material_snapshot(db, version_id, payload.country_iso3),
        )
        if version_id in selected_ids:
            if not _material_is_confirmed(db, version_id):
                raise HTTPException(422, "所选材料尚未复核，不能采用")
            candidate.decision = "accepted"
            candidate.reviewed_by = actor.id
            candidate.reviewed_at = datetime.now(UTC)
            db.add(
                ResearchCaseDocument(
                    research_case_id=research_case.id,
                    document_version_id=version_id,
                    added_by=actor.id,
                    usage_type="background",
                )
            )
            record_contribution(
                db,
                research_case_id=research_case.id,
                user_id=actor.id,
                action_type="evidence_reviewed",
                object_type="document_version",
                object_key=version_id,
                details={
                    "object_type": "document_version",
                    "object_id": version_id,
                    "decision": "accepted",
                    "note": "研究者在建项表单勾选采用",
                },
            )
    record_contribution(
        db,
        research_case_id=research_case.id,
        user_id=actor.id,
        action_type="research_case_created",
        object_type="research_case",
        object_key=research_case.id,
        details={"creation_request": payload.idempotency_key, "creation_fingerprint": request_hash},
    )
    db.commit()
    db.refresh(research_case)
    return {
        **_case_summary(db, research_case),
        "research_question": research_question,
        "scope": _public_case_scope(research_case.scope),
    }


def _field_material_summary(item: FieldMaterial) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "original_filename": item.original_filename,
        "content_type": item.content_type,
        "byte_size": item.byte_size,
        "sha256": item.sha256,
        "material_type": item.material_type,
        "privacy_level": item.privacy_level,
        "evidence_status": item.evidence_status,
        "country_iso3": item.country_iso3,
        "research_case_id": item.research_case_id,
        "captured_on": item.captured_on,
        "method_note": item.method_note,
        "created_at": item.created_at,
        "download_url": f"/api/v1/reader/field-materials/{item.id}/download",
    }


@router.post(
    "/field-materials",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def create_field_material(
    payload: FieldMaterialCreatePayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    country_iso3 = payload.country_iso3
    research_case = _active_research_case(db, payload.research_case_id) if payload.research_case_id else None
    if research_case:
        _case_permission(db, research_case.id, actor, "edit")
    case_country = (
        str((research_case.scope or {}).get("country_iso3") or "").upper() or None if research_case else None
    )
    if country_iso3 is not None and case_country is not None and country_iso3 != case_country:
        raise HTTPException(status_code=422, detail="field material country does not match topic scope")
    country_iso3 = country_iso3 or case_country
    if country_iso3 is not None:
        _require_catalog_country(country_iso3)
    try:
        stored = store_field_material(
            filename=payload.filename,
            reported_content_type=payload.content_type,
            content_base64=payload.content_base64,
        )
    except FieldMaterialValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = FieldMaterial(
        owner_id=actor.id,
        research_case_id=research_case.id if research_case else None,
        country_iso3=country_iso3,
        title=payload.title.strip(),
        original_filename=payload.filename.strip(),
        storage_key=stored.storage_key,
        content_type=stored.content_type,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        material_type=payload.material_type,
        privacy_level=payload.privacy_level,
        evidence_status="user_provided_unreviewed",
        authorization_confirmed=True,
        captured_on=payload.captured_on,
        method_note=payload.method_note.strip(),
    )
    db.add(item)
    if research_case:
        research_case.updated_at = datetime.now(UTC)
    db.flush()
    from app.services import field_access

    field_access.ensure_asset(db, item)
    if research_case:
        field_access.owner_project_link(db, item, actor.id, research_case.id)
        record_contribution(
            db,
            research_case_id=research_case.id,
            user_id=actor.id,
            action_type="field_material_uploaded",
            object_type="field_material",
            object_key=item.id,
            details={"privacy_level": item.privacy_level, "sha256": item.sha256},
        )
    field_access.audit(db, actor.id, item.id, "uploaded", country_iso3=country_iso3)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        if stored.created:
            stored.path.unlink(missing_ok=True)
        raise
    db.refresh(item)
    return _field_material_summary(item)


@router.get(
    "/field-materials",
    dependencies=[Depends(require_internal_reader_access)],
)
def list_field_materials(
    db: DbSession,
    actor: ReaderActor,
    country_iso3: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    research_case_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    from app.services import field_access

    if research_case_id is None:
        statement = select(FieldMaterial).where(FieldMaterial.owner_id == actor.id)
    else:
        member = _case_permission(db, research_case_id, actor, "view")
        statement = select(FieldMaterial).where(
            FieldMaterial.id.in_(field_access.project_material_ids(db, research_case_id))
        )
        if "view_restricted" not in ROLE_PERMISSIONS[member.role]:
            statement = statement.where(FieldMaterial.privacy_level != "restricted")
    if country_iso3 is not None:
        _require_catalog_country(country_iso3)
        statement = statement.where(FieldMaterial.country_iso3 == country_iso3)
    rows = db.scalars(
        statement.order_by(FieldMaterial.created_at.desc(), FieldMaterial.id.desc()).limit(limit)
    )
    result = []
    for item in rows:
        try:
            field_access.resolve_field_material_access(db, actor, item.id, research_case_id)
        except field_access.FieldAccessDenied:
            continue
        summary = _field_material_summary(item)
        if research_case_id:
            summary["project_id"] = research_case_id
            summary["download_url"] += f"?research_case_id={research_case_id}"
        result.append(summary)
    return result


@router.get(
    "/field-materials/{material_id}/download",
    dependencies=[Depends(require_internal_reader_access)],
)
def download_field_material(
    material_id: int,
    db: DbSession,
    actor: ReaderActor,
    research_case_id: Annotated[int | None, Query(gt=0)] = None,
) -> FileResponse:
    item = db.get(FieldMaterial, material_id)
    if item is None:
        raise HTTPException(status_code=404, detail="field material not found")
    from app.services import field_access

    project_id = research_case_id or (item.research_case_id if item.owner_id != actor.id else None)
    field_access.resolve_field_material_access(db, actor, item.id, project_id, "download", require_link=False)
    try:
        path = resolve_field_material_path(item.storage_key, expected_sha256=item.sha256)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="field material file is unavailable") from exc
    except FieldMaterialValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if project_id:
        record_contribution(
            db,
            research_case_id=project_id,
            user_id=actor.id,
            action_type="field_material_downloaded",
            object_type="field_material",
            object_key=item.id,
        )
    field_access.audit(db, actor.id, item.id, "downloaded", project_id=project_id)
    db.commit()
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=item.original_filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "/research-cases/{case_id}/events",
    dependencies=[Depends(require_reader_write_access)],
)
def add_research_case_event(
    case_id: int,
    payload: ResearchCaseEventPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    research_case = _active_research_case(db, case_id)
    _case_permission(db, case_id, actor, "edit")
    event = db.get(ResearchEvent, payload.event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    if event.review_status != "reviewed":
        raise HTTPException(status_code=422, detail="only reviewed events can be added")
    existing = db.scalar(
        select(ResearchCaseEvent).where(
            ResearchCaseEvent.research_case_id == case_id,
            ResearchCaseEvent.event_id == event.id,
        )
    )
    if existing is not None:
        return {"created": False, "research_case_id": case_id, "event_id": event.id}
    db.add(
        ResearchCaseEvent(
            research_case_id=case_id,
            event_id=event.id,
            usage_type=payload.usage_type,
        )
    )
    research_case.updated_at = datetime.now(UTC)
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="event_linked",
        object_type="event",
        object_key=event.id,
    )
    db.commit()
    return {"created": True, "research_case_id": case_id, "event_id": event.id}


@router.delete(
    "/research-cases/{case_id}/events",
    dependencies=[Depends(require_reader_write_access)],
)
def remove_research_case_event(
    case_id: int,
    payload: ResearchCaseEventPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    research_case = _active_research_case(db, case_id)
    _case_permission(db, case_id, actor, "edit")
    link = db.scalar(
        select(ResearchCaseEvent).where(
            ResearchCaseEvent.research_case_id == case_id,
            ResearchCaseEvent.event_id == payload.event_id,
        )
    )
    if link is None:
        return {"removed": False, "research_case_id": case_id, "event_id": payload.event_id}
    db.delete(link)
    research_case.updated_at = datetime.now(UTC)
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="event_unlinked",
        object_type="event",
        object_key=payload.event_id,
    )
    db.commit()
    return {"removed": True, "research_case_id": case_id, "event_id": payload.event_id}


@router.post(
    "/research-cases/{case_id}/materials",
    dependencies=[Depends(require_reader_write_access)],
)
def add_research_case_material(
    case_id: int,
    payload: ResearchCaseMaterialPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    research_case = _active_research_case(db, case_id)
    _case_permission(db, case_id, actor, "edit")
    version = db.get(DocumentVersion, payload.document_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="document version not found")
    if not _material_is_confirmed(db, version.id):
        raise HTTPException(status_code=422, detail="only confirmed materials can be added")
    existing = db.scalar(
        select(ResearchCaseDocument).where(
            ResearchCaseDocument.research_case_id == case_id,
            ResearchCaseDocument.document_version_id == version.id,
        )
    )
    if existing is not None:
        return {
            "created": False,
            "research_case_id": case_id,
            "document_id": version.document_id,
            "document_version_id": version.id,
        }
    db.add(
        ResearchCaseDocument(
            research_case_id=case_id,
            document_version_id=version.id,
            added_by=actor.id,
            usage_type=payload.usage_type,
        )
    )
    research_case.updated_at = datetime.now(UTC)
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="material_linked",
        object_type="document_version",
        object_key=version.id,
    )
    db.commit()
    return {
        "created": True,
        "research_case_id": case_id,
        "document_id": version.document_id,
        "document_version_id": version.id,
    }


@router.post(
    "/research-cases/{case_id}/data-slices",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def add_research_case_data_slice(
    case_id: int,
    payload: ResearchCaseDataSlicePayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    research_case = _active_research_case(db, case_id)
    _case_permission(db, case_id, actor, "edit")
    dataset = db.get(StructuredDataset, payload.dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="structured dataset not found")
    if payload.snapshot_id is not None:
        snapshot = db.get(StructuredSnapshot, payload.snapshot_id)
        if snapshot is None or snapshot.dataset_id != dataset.id:
            raise HTTPException(status_code=422, detail="snapshot does not belong to dataset")

    project_country = str((research_case.scope or {}).get("country_iso3") or "").upper()
    filter_country = str(payload.filter_spec.get("country_iso3") or "").upper()
    if not project_country or filter_country != project_country:
        raise HTTPException(status_code=422, detail="data slice country must match project country")
    indicator_code = str(payload.filter_spec.get("indicator_code") or "").strip()
    metric_code = str(payload.filter_spec.get("metric_code") or "").strip()
    if not indicator_code and not metric_code:
        raise HTTPException(status_code=422, detail="data slice must identify an indicator")
    observation_statement = select(StructuredObservation.id).where(
        StructuredObservation.dataset_id == dataset.id,
        StructuredObservation.country_iso3 == project_country,
    )
    if indicator_code:
        observation_statement = observation_statement.where(
            StructuredObservation.indicator_code == indicator_code
        )
    if metric_code:
        observation_statement = observation_statement.where(StructuredObservation.metric_code == metric_code)
    if db.scalar(observation_statement.limit(1)) is None:
        raise HTTPException(status_code=422, detail="data slice has no matching saved observations")

    label = payload.label.strip()
    existing = db.scalar(
        select(ResearchCaseDataSlice).where(
            ResearchCaseDataSlice.research_case_id == case_id,
            ResearchCaseDataSlice.label == label,
        )
    )
    if existing is not None:
        return {
            "created": False,
            "research_case_id": case_id,
            "data_slice_id": existing.id,
        }
    data_slice = ResearchCaseDataSlice(
        research_case_id=case_id,
        dataset_id=dataset.id,
        snapshot_id=payload.snapshot_id,
        label=label,
        filter_spec=payload.filter_spec,
    )
    db.add(data_slice)
    db.flush()
    research_case.updated_at = datetime.now(UTC)
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="data_slice_linked",
        object_type="research_case_data_slice",
        object_key=data_slice.id,
    )
    db.commit()
    return {
        "created": True,
        "research_case_id": case_id,
        "data_slice_id": data_slice.id,
    }


@router.delete(
    "/research-cases/{case_id}/materials/{document_id}",
    dependencies=[Depends(require_reader_write_access)],
)
def remove_research_case_material(
    case_id: int,
    document_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    research_case = _active_research_case(db, case_id)
    _case_permission(db, case_id, actor, "edit")
    links = list(
        db.scalars(
            select(ResearchCaseDocument)
            .join(DocumentVersion, DocumentVersion.id == ResearchCaseDocument.document_version_id)
            .where(
                ResearchCaseDocument.research_case_id == case_id,
                DocumentVersion.document_id == document_id,
            )
        )
    )
    if not links:
        return {"removed": False, "research_case_id": case_id, "document_id": document_id}
    for link in links:
        db.delete(link)
    research_case.updated_at = datetime.now(UTC)
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="material_unlinked",
        object_type="document",
        object_key=document_id,
    )
    db.commit()
    return {"removed": True, "research_case_id": case_id, "document_id": document_id}


@router.get("/research-cases")
def list_research_cases(
    db: DbSession,
    actor: ReaderActor,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    statement = select(ResearchCase).where(ResearchCase.status == "active")
    if _reader_identity_initialized(db):
        statement = statement.join(
            ResearchCaseMember, ResearchCaseMember.research_case_id == ResearchCase.id
        ).where(ResearchCaseMember.user_id == actor.id)
    else:
        statement = statement.where(ResearchCase.owner_id == actor.id)
    cases = db.scalars(
        statement.order_by(ResearchCase.updated_at.desc(), ResearchCase.id.desc()).limit(limit)
    )
    return [
        {**_case_summary(db, research_case), "can_delete": research_case.owner_id == actor.id}
        for research_case in cases
    ]


@router.get("/research-cases/{case_id}")
def get_research_case(
    case_id: int,
    db: DbSession,
    actor: ReaderActor,
    q: Annotated[str | None, Query(max_length=200)] = None,
    source_type: Annotated[str | None, Query(max_length=80)] = None,
    document_type: Annotated[str | None, Query(max_length=80)] = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
) -> dict[str, Any]:
    research_case = db.get(ResearchCase, case_id)
    if research_case is None:
        raise HTTPException(status_code=404, detail="research case not found")
    current_member = _case_permission(db, case_id, actor, "view")
    event_rows = list(
        db.execute(
            select(ResearchCaseEvent, ResearchEvent)
            .join(ResearchEvent, ResearchEvent.id == ResearchCaseEvent.event_id)
            .where(
                ResearchCaseEvent.research_case_id == case_id,
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(ResearchEvent.start_at, ResearchEvent.id)
        )
    )
    document_statement = (
        select(ResearchCaseDocument, DocumentVersion, Document, Source)
        .join(DocumentVersion, DocumentVersion.id == ResearchCaseDocument.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            ResearchCaseDocument.research_case_id == case_id,
            or_(
                DocumentVersion.id.in_(
                    select(DocumentEntity.document_version_id).where(
                        DocumentEntity.review_status == "confirmed"
                    )
                ),
                DocumentVersion.id.in_(
                    select(EventMention.document_version_id).where(EventMention.review_status == "confirmed")
                ),
            ),
        )
    )
    if q:
        pattern = f"%{q.strip()}%"
        document_statement = document_statement.where(
            or_(
                DocumentVersion.title.ilike(pattern),
                Document.title.ilike(pattern),
                Source.name.ilike(pattern),
            )
        )
    if source_type:
        document_statement = document_statement.where(Source.source_type == source_type)
    if document_type:
        document_statement = document_statement.where(Document.document_type == document_type)
    effective_published_at = func.coalesce(DocumentVersion.published_at, Document.published_at)
    if published_from:
        document_statement = document_statement.where(effective_published_at >= published_from)
    if published_to:
        document_statement = document_statement.where(effective_published_at <= published_to)
    document_rows = list(
        db.execute(document_statement.order_by(effective_published_at.desc(), DocumentVersion.id.desc()))
    )
    document_groups: dict[int, list[tuple[ResearchCaseDocument, DocumentVersion, Document, Source]]] = (
        defaultdict(list)
    )
    for row in document_rows:
        document_groups[row[2].id].append(row)
    logical_documents = []
    for group in document_groups.values():
        selected = max(group, key=lambda row: (row[1].version_no, row[1].id))
        link, version, document, source = selected
        logical_documents.append(
            {
                **_document_summary(version, document, source),
                "usage_type": link.usage_type,
                "note": link.note,
                "linked_version_ids": sorted({row[1].id for row in group}),
                "linked_version_count": len({row[1].id for row in group}),
            }
        )
    logical_documents.sort(
        key=lambda item: (_iso_datetime(item["published_at"]) or "", item["document_version_id"]),
        reverse=True,
    )
    slices = list(
        db.scalars(
            select(ResearchCaseDataSlice)
            .where(ResearchCaseDataSlice.research_case_id == case_id)
            .order_by(ResearchCaseDataSlice.id)
        )
    )
    runs = list(
        db.scalars(
            select(CapabilityRun)
            .where(CapabilityRun.research_case_id == case_id)
            .order_by(CapabilityRun.created_at.desc(), CapabilityRun.id.desc())
        )
    )
    return {
        **_case_summary(db, research_case),
        "research_question": research_case.research_question,
        "scope": _public_case_scope(research_case.scope),
        "scope_revision": int((research_case.scope or {}).get("scope_revision", 1)),
        "context_bindings": (research_case.scope or {}).get("context_bindings", {}),
        "current_member": public_member_payload(current_member, actor),
        "permission_matrix": {role: sorted(value) for role, value in ROLE_PERMISSIONS.items()},
        "lifecycle": {"status": research_case.status, "archived": research_case.status == "archived"},
        "events": [
            {**_event_summary(event), "usage_type": link.usage_type, "note": link.note}
            for link, event in event_rows
        ],
        "documents": logical_documents,
        "material_facets": {
            "source_types": sorted({source.source_type for _, _, _, source in document_rows}),
            "document_types": sorted({document.document_type for _, _, document, _ in document_rows}),
        },
        "data_slices": [
            {
                "id": item.id,
                "dataset_id": item.dataset_id,
                "snapshot_id": item.snapshot_id,
                "label": item.label,
                "filter_spec": item.filter_spec,
            }
            for item in slices
        ],
        "capability_runs": [_run_summary(run) for run in runs],
        "task_summary": {
            "conversation_count": db.scalar(
                select(func.count(func.distinct(AgentRun.conversation_id))).where(
                    AgentRun.scope_type == "topic", AgentRun.scope_key == str(case_id)
                )
            )
            or 0,
            "capability_run_count": len(runs),
            "running_count": sum(run.status in {"queued", "running"} for run in runs),
        },
        "pending_review_count": sum(
            run.status == "succeeded"
            and not db.scalar(
                select(CapabilityRunTarget.id).where(CapabilityRunTarget.capability_run_id == run.id)
            )
            for run in runs
        ),
        "formal_output_count": db.scalar(
            select(func.count(CapabilityRunTarget.id))
            .join(CapabilityRun, CapabilityRun.id == CapabilityRunTarget.capability_run_id)
            .where(CapabilityRun.research_case_id == case_id)
        )
        or 0,
        "workflow": _research_case_workflow(
            db,
            research_case,
            event_count=len(event_rows),
            document_count=len(logical_documents),
            data_slice_count=len(slices),
        ),
    }


class ResearchCaseContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    country_iso3: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    event_ids: list[int] = Field(default_factory=list, max_length=100)
    document_version_ids: list[int] = Field(default_factory=list, max_length=500)
    data_slice_ids: list[int] = Field(default_factory=list, max_length=100)
    field_material_ids: list[int] = Field(default_factory=list, max_length=100)


class ProjectTaskStreamPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = Field(default="chat", pattern=r"^(plan|chat)$")
    answers: dict[str, str] = Field(default_factory=dict)
    expected_revision: int = Field(default=0, ge=0)
    plan_revision_id: int | None = Field(default=None, gt=0)
    request_id: str | None = Field(default=None, min_length=8, max_length=64)

    conversation_id: str | None = Field(default=None, min_length=8, max_length=36)
    question: str = Field(min_length=1, max_length=12000)
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    capability_config_id: int | None = Field(default=None, gt=0)
    capability_config_ids: list[int] = Field(default_factory=list, max_length=3)
    online_mode: str = Field(default="off", pattern=r"^(off|auto|on)$")

    @model_validator(mode="after")
    def normalize_capability_selection(self) -> ProjectTaskStreamPayload:
        if self.mode == "chat" and len(self.question) > 500:
            raise ValueError("普通对话最多500字，请使用方向梳理编辑长提示词")
        selected_ids = list(dict.fromkeys(self.capability_config_ids))
        if self.capability_config_id and self.capability_config_id not in selected_ids:
            selected_ids.insert(0, self.capability_config_id)
        if len(selected_ids) > 3:
            raise ValueError("at most 3 capability configurations can be selected")
        self.capability_config_ids = selected_ids
        self.capability_config_id = selected_ids[0] if selected_ids else None
        return self


class EvidenceReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_type: str = Field(pattern=r"^(direct|field|inference|gap)$")
    object_type: str = Field(pattern=r"^(document_version|event_mention|field_material|capability_run|gap)$")
    object_id: int | None = Field(default=None, gt=0)
    decision: str = Field(pattern=r"^(accepted|rejected|needs_revision)$")
    note: str = Field(default="", max_length=2000)
    linked_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class ResearchExportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: str = Field(pattern=r"^(markdown|json|csv)$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,64}$")


def _research_export_content(artifact: dict[str, Any], export_format: str) -> tuple[str, str]:
    serialized = json.dumps(artifact, ensure_ascii=False, indent=2, default=str)
    if export_format == "markdown":
        return project_workflow.readable_markdown(artifact), "text/markdown;charset=utf-8"
    if export_format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["field", "value"])
        for key, value in artifact.items():
            writer.writerow(
                [
                    key,
                    json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, (dict, list))
                    else str(value),
                ]
            )
        return output.getvalue(), "text/csv;charset=utf-8"
    return serialized, "application/json;charset=utf-8"


@router.patch(
    "/research-cases/{case_id}/context",
    dependencies=[Depends(require_reader_write_access)],
)
def update_research_case_context(
    case_id: int,
    payload: ResearchCaseContextPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "edit")
    research_case = db.get(ResearchCase, case_id)
    if research_case is None:
        raise HTTPException(status_code=404, detail="research case not found")
    before = dict(research_case.scope or {})
    revision = int(before.get("scope_revision", 1))
    if revision != payload.expected_revision:
        raise HTTPException(
            status_code=409, detail={"code": "scope_revision_conflict", "current_revision": revision}
        )
    bindings = payload.model_dump(mode="json", exclude={"expected_revision"})
    if payload.event_ids:
        reviewed_count = db.scalar(
            select(func.count(ResearchEvent.id)).where(
                ResearchEvent.id.in_(set(payload.event_ids)), ResearchEvent.review_status == "reviewed"
            )
        )
        if reviewed_count != len(set(payload.event_ids)):
            raise HTTPException(status_code=422, detail="project context contains unavailable events")
    if payload.document_version_ids and any(
        not _material_is_confirmed(db, version_id) for version_id in set(payload.document_version_ids)
    ):
        raise HTTPException(status_code=422, detail="project context contains unconfirmed materials")
    after = {**before, "scope_revision": revision + 1, "context_bindings": bindings}
    if payload.country_iso3:
        after["country_iso3"] = payload.country_iso3
    research_case.scope = after
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="context_changed",
        object_type="research_case_scope",
        object_key=case_id,
        details={"before": before, "after": after},
    )
    db.commit()
    return {"research_case_id": case_id, "scope": after, "scope_revision": revision + 1}


def _project_task_rows(db: Session, case_id: int) -> list[dict[str, Any]]:
    agent_runs = list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.scope_type == "topic", AgentRun.scope_key == str(case_id))
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        )
    )
    grouped: dict[str, list[AgentRun]] = defaultdict(list)
    for run in agent_runs:
        if (run.scope_context or {}).get("capability_run_id"):
            continue  # Generated reports belong in the result shelf, not conversation history.
        grouped[run.conversation_id or f"run-{run.id}"].append(run)
    tasks = [
        {
            "id": conversation_id,
            "kind": "conversation",
            "title": project_workflow.short_title(runs[-1].question, db.get(ResearchCase, case_id).title),
            "status": runs[0].status,
            "turn_count": len(runs),
            "latest_run_id": runs[0].id,
            "updated_at": runs[0].updated_at,
        }
        for conversation_id, runs in grouped.items()
    ]
    for run in db.scalars(
        select(CapabilityRun)
        .where(CapabilityRun.research_case_id == case_id)
        .order_by(CapabilityRun.created_at.desc(), CapabilityRun.id.desc())
    ):
        tasks.append(
            {
                "id": f"capability-{run.id}",
                "kind": "capability_run",
                "title": f"能力运行 #{run.id}",
                "status": run.status,
                "turn_count": 1,
                "capability_run_id": run.id,
                "updated_at": run.updated_at,
            }
        )
    managed = list(db.scalars(select(ProjectTask).where(ProjectTask.research_case_id == case_id)))
    managed_ids = {task.id for task in managed}
    tasks = [item for item in tasks if item["id"] not in managed_ids]
    tasks.extend(project_workflow.task_payload(db, task) for task in managed)
    deleted = project_workflow.deleted_conversation_ids(db, case_id)
    return sorted(
        [item for item in tasks if item["id"] not in deleted],
        key=lambda item: str(item["updated_at"]),
        reverse=True,
    )


def _validated_project_references(
    db: Session, research_case: ResearchCase, references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    country_iso3 = str((research_case.scope or {}).get("country_iso3") or "").upper()
    decisions = project_workflow.adoption_decisions(db, research_case.id)
    allowed_mentions = project_workflow.adopted_mention_ids(db, research_case.id)
    for reference in references:
        kind = str(reference.get("type") or "")
        raw_id = reference.get("id")
        label = str(reference.get("label") or "")[:160]
        if kind == "country":
            if str(raw_id or "").upper() != country_iso3:
                raise HTTPException(status_code=422, detail="country reference is outside project scope")
            validated.append({"type": kind, "id": country_iso3, "label": label})
            continue
        if not str(raw_id or "").isdigit():
            raise HTTPException(status_code=422, detail="project reference requires a numeric id")
        object_id = int(raw_id)
        if kind == "structured_observation_version":
            from app.services.project_candidates import adopted_observations

            scope = research_case.scope or {}
            countries = scope.get("country_iso3s") or ([country_iso3] if country_iso3 else None)
            _, locators = adopted_observations(db, research_case.id, countries)
            locator = locators.get(object_id)
            if locator is None:
                raise HTTPException(status_code=422, detail="结构化数据尚未采用、超出项目范围或当前不可引用")
            if str(reference.get("snapshot_id") or "") != str(locator["snapshot_id"]):
                raise HTTPException(status_code=422, detail="结构化引用必须匹配项目采用的快照")
            validated.append({"type": kind, "id": object_id, "label": label, **locator})
            continue
        exists = False
        if kind == "event":
            exists = bool(
                db.scalar(
                    select(ResearchCaseEvent.id).where(
                        ResearchCaseEvent.research_case_id == research_case.id,
                        ResearchCaseEvent.event_id == object_id,
                    )
                )
            )
        elif kind in {"material", "document_version"}:
            exists = bool(
                db.scalar(
                    select(ResearchCaseDocument.id).where(
                        ResearchCaseDocument.research_case_id == research_case.id,
                        ResearchCaseDocument.document_version_id == object_id,
                    )
                )
            )
            kind = "document_version"
        elif kind == "data_slice":
            exists = bool(
                db.scalar(
                    select(ResearchCaseDataSlice.id).where(
                        ResearchCaseDataSlice.research_case_id == research_case.id,
                        ResearchCaseDataSlice.id == object_id,
                    )
                )
            )
        elif kind == "field_material":
            exists = object_id in project_workflow.adopted_field_material_ids(db, research_case.id)
        else:
            raise HTTPException(status_code=422, detail="unsupported project reference type")
        if not exists:
            raise HTTPException(status_code=422, detail=f"{kind} reference is outside project scope")
        if kind == "event":
            adopted = bool(
                db.scalar(
                    select(EventMention.id)
                    .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
                    .where(
                        EventMention.event_id == object_id,
                        EventMention.id.in_(allowed_mentions),
                        ResearchEvent.review_status == "reviewed",
                    )
                )
            )
        elif kind == "document_version":
            adopted = project_workflow.adopted_document(db, decisions, object_id)
        else:
            adopted = project_workflow.is_adopted(decisions, kind, object_id)
        if not adopted:
            raise HTTPException(status_code=422, detail="资料尚未采用、已被排除或当前不可引用")
        validated.append({"type": kind, "id": object_id, "label": label})
    return validated


@router.get("/research-cases/{case_id}/tasks")
def list_research_case_tasks(case_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "view")
    tasks = _project_task_rows(db, case_id)
    return {"items": tasks, "count": len(tasks)}


@router.get("/research-cases/{case_id}/tasks/{task_id}")
def get_research_case_task(case_id: int, task_id: str, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "view")
    if task_id in project_workflow.deleted_conversation_ids(db, case_id):
        raise HTTPException(404, "对话已删除")
    managed = db.get(ProjectTask, task_id)
    if managed is not None:
        if managed.research_case_id != case_id:
            raise HTTPException(status_code=404, detail="project task not found")
        from app.api.project_skill_turns import recover_interrupted

        managed = recover_interrupted(db, managed)
        return project_workflow.task_payload(db, managed)
    if task_id.startswith("capability-") and task_id.removeprefix("capability-").isdigit():
        run = db.get(CapabilityRun, int(task_id.removeprefix("capability-")))
        if run is None or run.research_case_id != case_id:
            raise HTTPException(status_code=404, detail="project task not found")
        return {"id": task_id, "kind": "capability_run", "run": get_capability_run(run.id, db, actor)}
    runs = list(
        db.scalars(
            select(AgentRun)
            .where(
                AgentRun.conversation_id == task_id,
                AgentRun.scope_type == "topic",
                AgentRun.scope_key == str(case_id),
            )
            .order_by(AgentRun.created_at, AgentRun.id)
        )
    )
    if not runs:
        raise HTTPException(status_code=404, detail="project task not found")
    return {
        "id": task_id,
        "kind": "conversation",
        "turns": [
            {
                "run_id": run.id,
                "question": run.question,
                "status": run.status,
                "artifact": run.artifact,
                "created_at": run.created_at,
            }
            for run in runs
        ],
    }


@router.post("/research-cases/{case_id}/tasks/stream")
def stream_research_case_task(
    case_id: int,
    payload: ProjectTaskStreamPayload,
    db: DbSession,
    actor: ReaderActor,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> StreamingResponse:
    _case_permission(db, case_id, actor, "edit")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    if payload.conversation_id in project_workflow.deleted_conversation_ids(db, case_id):
        raise HTTPException(404, "对话已删除，请新建研究")
    managed_task = db.get(ProjectTask, payload.conversation_id) if payload.conversation_id else None
    if managed_task and any(
        m.get("skill_turn", {}).get("status") in {"queued", "running", "pausing"}
        for m in managed_task.messages
    ):
        raise HTTPException(409, "此会话正在调用 Skill，请等待本轮完成")
    if payload.mode == "plan":
        from app.api.project_workspace import stream_plan

        return stream_plan(case_id, payload, db, actor)
    if payload.online_mode != "off":
        raise HTTPException(status_code=422, detail="项目研究仅使用国别空间数据库")
    _case_permission(db, case_id, actor, "edit")
    research_case = db.get(ResearchCase, case_id)
    if research_case is None:
        raise HTTPException(status_code=404, detail="research case not found")
    for config_id in payload.capability_config_ids:
        config = db.get(CapabilityConfig, config_id)
        template = db.get(CapabilityTemplate, config.template_id) if config else None
        if config is None or not _capability_config_visible(db, config, actor, "run_skill"):
            raise HTTPException(status_code=403, detail="capability config is outside this project")
        if config.research_case_id and config.research_case_id != case_id:
            raise HTTPException(status_code=403, detail="capability config is outside this project")
        if (
            config.status not in {"active", "published"}
            or template is None
            or template.status != "active"
            or template.validation_status != "verified"
        ):
            raise HTTPException(status_code=409, detail="selected capability is not runnable")
    references = _validated_project_references(db, research_case, payload.references)
    context = AssistantContextPayload(
        space="project",
        country_iso3=(research_case.scope or {}).get("country_iso3", "COD"),
        research_case_id=case_id,
        capability_config_ids=payload.capability_config_ids,
        focus_type="structured_references",
        focus_key=str(case_id),
        focus_label=research_case.title,
    )
    assistant_payload = AssistantQuestionPayload(
        conversation_id=payload.conversation_id,
        context=context,
        question=payload.question,
        online_mode=payload.online_mode,
    )
    _require_assistant_case_access(db, case_id, x_reader_key, "edit")
    agent_run = _create_assistant_run(db, assistant_payload, requested_by=actor.id)
    agent_run.scope_context = {
        **(agent_run.scope_context or {}),
        "structured_references": references,
    }
    db.commit()
    worker_sessions = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    return StreamingResponse(
        _assistant_event_stream(
            worker_sessions,
            payload=assistant_payload,
            run_id=agent_run.id,
            conversation_id=agent_run.conversation_id or "",
            scope_type=agent_run.scope_type,
            scope_key=agent_run.scope_key,
            runtime=agent_run.runtime,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/research-cases/{case_id}/tasks/{task_id}/cancel")
def cancel_research_case_task(
    case_id: int,
    task_id: str,
    db: DbSession,
    actor: ReaderActor,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "edit")
    task = db.scalar(
        select(ProjectTask)
        .where(ProjectTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is not None:
        if task.research_case_id != case_id:
            raise HTTPException(status_code=404, detail="project task not found")
        if any(
            m.get("skill_turn", {}).get("status") in {"queued", "running", "pausing"} for m in task.messages
        ):
            from app.api.project_skill_turns import pause_skill

            return pause_skill(case_id, task_id, db, actor)
        if task.status != "running" and not project_workflow.plan_in_progress(task):
            db.commit()
            return project_workflow.task_payload(db, task)
        task.status = "cancelled"
        plan = project_workflow.get_plan(db, task)
        if plan and plan.content.get("retrieval", {}).get("status") == "running":
            plan.content = {
                **plan.content,
                "retrieval": {**plan.content["retrieval"], "status": "cancelled"},
            }
        project_workflow.append_message(task, "assistant", "任务已停止；已保存的讨论与候选资料可以继续查看。")
        db.commit()
        return project_workflow.task_payload(db, task)
    run = db.scalar(
        select(AgentRun)
        .where(
            AgentRun.conversation_id == task_id,
            AgentRun.scope_type == "topic",
            AgentRun.scope_key == str(case_id),
        )
        .order_by(AgentRun.id.desc())
        .limit(1)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="project task not found")
    return cancel_assistant_stream_run(run.id, db, x_reader_key)


@router.get("/research-cases/{case_id}/evidence")
def get_research_case_evidence(case_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "view")
    direct = list(
        db.scalars(select(ResearchCaseDocument).where(ResearchCaseDocument.research_case_id == case_id))
    )
    event_mentions = list(
        db.execute(
            select(EventMention, ResearchCaseEvent)
            .join(ResearchCaseEvent, ResearchCaseEvent.event_id == EventMention.event_id)
            .where(
                ResearchCaseEvent.research_case_id == case_id,
                EventMention.review_status == "confirmed",
            )
        )
    )
    field = list_field_materials(db, actor, research_case_id=case_id, country_iso3=None, limit=100)
    reviews = list(
        db.scalars(
            select(ResearchContribution)
            .where(
                ResearchContribution.research_case_id == case_id,
                ResearchContribution.action_type == "evidence_reviewed",
            )
            .order_by(ResearchContribution.created_at.desc())
        )
    )
    return {
        "groups": {
            "direct": [
                {
                    "object_type": "document_version",
                    "object_id": item.document_version_id,
                    "usage_type": item.usage_type,
                    "note": item.note,
                }
                for item in direct
            ]
            + [
                {
                    "object_type": "event_mention",
                    "object_id": mention.id,
                    "event_id": link.event_id,
                    "locator": mention.evidence_locator,
                }
                for mention, link in event_mentions
            ],
            "field": [
                {
                    "object_type": "field_material",
                    "object_id": item["id"],
                    "title": item["title"],
                    "privacy_level": item["privacy_level"],
                    "authorization_confirmed": True,
                }
                for item in field
            ],
            "inference": [],
            "gap": [],
        },
        "reviews": [
            item.details
            for item in reviews
            if item.details.get("object_type") != "field_material"
            or item.details.get("object_id") in {entry["id"] for entry in field}
        ],
        "rule": "project review changes adoption only; global review state is unchanged",
    }


@router.post(
    "/research-cases/{case_id}/evidence-reviews",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def review_research_case_evidence(
    case_id: int,
    payload: EvidenceReviewPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "revise")
    if payload.evidence_type == "inference" and not payload.linked_evidence:
        raise HTTPException(status_code=422, detail="inference must link direct or field evidence")
    compatible_types = {
        "direct": {"document_version", "event_mention"},
        "field": {"field_material"},
        "inference": {"capability_run"},
        "gap": {"gap"},
    }
    if payload.object_type not in compatible_types[payload.evidence_type]:
        raise HTTPException(status_code=422, detail="evidence type and object type are incompatible")
    if payload.object_type == "document_version" and not db.scalar(
        select(ResearchCaseDocument.id).where(
            ResearchCaseDocument.research_case_id == case_id,
            ResearchCaseDocument.document_version_id == payload.object_id,
        )
    ):
        raise HTTPException(status_code=422, detail="document evidence is outside this project")
    if payload.object_type == "field_material":
        from app.services.field_access import resolve_field_material_access

        resolve_field_material_access(db, actor, payload.object_id, case_id, "cite")
    if payload.object_type == "capability_run" and not db.scalar(
        select(CapabilityRun.id).where(
            CapabilityRun.research_case_id == case_id,
            CapabilityRun.id == payload.object_id,
        )
    ):
        raise HTTPException(status_code=422, detail="inference is outside this project")
    if payload.object_type == "event_mention":
        mention = db.get(EventMention, payload.object_id)
        linked = mention and db.scalar(
            select(ResearchCaseEvent.id).where(
                ResearchCaseEvent.research_case_id == case_id,
                ResearchCaseEvent.event_id == mention.event_id,
            )
        )
        if mention is None or mention.review_status != "confirmed" or not linked:
            raise HTTPException(
                status_code=422, detail="event evidence must be a confirmed mention of a project event"
            )
    candidate = db.scalar(
        select(ProjectEvidence).where(
            ProjectEvidence.research_case_id == case_id,
            ProjectEvidence.object_type == payload.object_type,
            ProjectEvidence.object_id == payload.object_id,
        )
    )
    if candidate:
        candidate.decision, candidate.note = payload.decision, payload.note
        candidate.reviewed_by, candidate.reviewed_at = actor.id, datetime.now(UTC)
    details = payload.model_dump(mode="json")
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="evidence_reviewed",
        object_type=payload.object_type,
        object_key=payload.object_id or "gap",
        details=details,
    )
    db.commit()
    return {"research_case_id": case_id, "review": details, "global_state_unchanged": True}


@router.get("/research-cases/{case_id}/outputs")
def list_research_case_outputs(case_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "view")
    rows = list(
        db.execute(
            select(CapabilityRunTarget, CapabilityRun)
            .join(CapabilityRun, CapabilityRun.id == CapabilityRunTarget.capability_run_id)
            .where(CapabilityRun.research_case_id == case_id)
            .order_by(CapabilityRunTarget.confirmed_at.desc(), CapabilityRunTarget.id.desc())
        )
    )
    from app.api.project_workspace import (
        _usable_output_evidence,
        _validate_legacy_output_references,
        _validate_output_citations,
    )

    usable = None

    def usable_evidence():
        nonlocal usable
        if usable is None:
            usable = _usable_output_evidence(case_id, db, actor)
        return usable

    items = []
    for target, run in rows:
        from app.services import field_access

        try:
            field_access.enforce_run_access(db, run, actor)
        except field_access.FieldAccessDenied:
            items.append(
                {
                    "target_id": target.id,
                    "capability_run_id": run.id,
                    "target_type": target.target_type,
                    "artifact_snapshot": None,
                    "availability": {
                        "status": "needs_rights_review",
                        "reason": "资料授权已变，成果锁定待复核",
                    },
                    "document_title": None,
                    "document_version": None,
                    "confirmed_at": target.confirmed_at,
                }
            )
            continue
        snapshot = target.artifact_snapshot
        document = (snapshot.get("output") or {}).get("document")
        availability = {"status": "legacy_unverified", "reason": "历史格式尚未完成当前引用复核"}
        workflow_output = snapshot.get("type") in {"field_research_workflow", "policy_tracking_workflow"}
        if workflow_output:
            from app.services.workflow_titles import workflow_result_title

            snapshot = {
                **snapshot,
                "title": workflow_result_title(snapshot, run.input_snapshot.get("config", {})),
            }
            availability = {"status": "available", "reason": "已保存工作流结果；原文访问权限已复核"}
        elif document is not None:
            try:
                _validate_output_citations(document.get("citations", []), usable_evidence())
                availability = {"status": "available", "reason": "引用已按当前采用状态与许可复核"}
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                availability = {"status": "needs_review", "reason": str(exc.detail)}
                snapshot = None
        else:
            try:
                _validate_legacy_output_references(snapshot.get("output", snapshot), case_id, db)
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                availability = {"status": "needs_review", "reason": str(exc.detail)}
                snapshot = None
        items.append(
            {
                "target_id": target.id,
                "capability_run_id": run.id,
                "target_type": target.target_type,
                "artifact_snapshot": snapshot,
                "availability": availability,
                "document_title": document.get("title")
                if document
                else (snapshot.get("title") if workflow_output else None),
                "document_version": document.get("version")
                if document
                else (snapshot.get("saved_revision_no") if snapshot else None),
                "confirmed_by": target.confirmed_by,
                "confirmed_at": target.confirmed_at,
            }
        )
    saved_run_ids = {run.id for _, run in rows}
    drafts = []
    for run in db.scalars(
        select(CapabilityRun)
        .where(CapabilityRun.research_case_id == case_id)
        .order_by(CapabilityRun.id.desc())
    ):
        document = (run.output or {}).get("document")
        if (run.output or {}).get("type") in {"field_research_workflow", "policy_tracking_workflow"}:
            from app.services.workflow_documents import latest as latest_workflow_document

            revision = latest_workflow_document(db, run.id)
            if revision and revision.artifact["state"] != "confirmed":
                from app.services import field_access

                try:
                    field_access.enforce_run_access(db, run, actor)
                    drafts.append(
                        {
                            "run_id": run.id,
                            "title": revision.artifact["document"]["title"],
                            "workflow_document": True,
                            "availability": "available",
                        }
                    )
                except field_access.FieldAccessDenied:
                    drafts.append(
                        {
                            "run_id": run.id,
                            "title": None,
                            "workflow_document": True,
                            "availability": "needs_rights_review",
                        }
                    )
            continue
        if not document or run.id in saved_run_ids:
            continue
        from app.services import field_access

        try:
            field_access.enforce_run_access(db, run, actor)
        except field_access.FieldAccessDenied:
            drafts.append(
                {
                    "run_id": run.id,
                    "title": None,
                    "material_type": None,
                    "availability": "needs_rights_review",
                }
            )
            continue
        availability = "available"
        try:
            _validate_output_citations(document.get("citations", []), usable_evidence())
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            availability = "needs_review"
        drafts.append(
            {
                "run_id": run.id,
                "title": document.get("title"),
                "material_type": document.get("material_type"),
                "availability": availability,
            }
        )
    return {"items": items, "count": len(items), "drafts": drafts}


@router.get("/research-cases/{case_id}/outputs/{target_id}")
def get_research_case_output(
    case_id: int, target_id: int, db: DbSession, actor: ReaderActor
) -> dict[str, Any]:
    payload = list_research_case_outputs(case_id, db, actor)
    item = next((row for row in payload["items"] if row["target_id"] == target_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="project output not found")
    return item


@router.post(
    "/research-cases/{case_id}/outputs/{target_id}/exports",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def export_research_case_output(
    case_id: int,
    target_id: int,
    payload: ResearchExportPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "confirm")
    target = db.get(CapabilityRunTarget, target_id)
    run = db.get(CapabilityRun, target.capability_run_id) if target else None
    if target is None or run is None or run.research_case_id != case_id:
        raise HTTPException(status_code=404, detail="project output not found")
    from app.api.project_workspace import (
        _usable_output_evidence,
        _validate_legacy_output_references,
        _validate_output_citations,
    )

    _require_capability_run_access(db, run, actor, "confirm")
    document = (target.artifact_snapshot.get("output") or {}).get("document")
    if document is not None:
        _validate_output_citations(document.get("citations", []), _usable_output_evidence(case_id, db, actor))
    else:
        _validate_legacy_output_references(
            target.artifact_snapshot.get("output", target.artifact_snapshot), case_id, db
        )
    existing = db.scalar(
        select(ResearchExport).where(ResearchExport.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        if (
            existing.research_case_id != case_id
            or existing.capability_run_target_id != target_id
            or existing.format != payload.format
        ):
            raise HTTPException(status_code=409, detail="导出请求标识已用于其他成果或格式")
        frozen_document = (((existing.artifact or {}).get("output") or {}).get("output") or {}).get(
            "document"
        )
        if frozen_document is not None:
            _validate_output_citations(
                frozen_document.get("citations", []), _usable_output_evidence(case_id, db, actor)
            )
        else:
            frozen_output = (existing.artifact or {}).get("output") or {}
            _validate_legacy_output_references(frozen_output.get("output", frozen_output), case_id, db)
        content, media_type = _research_export_content(existing.artifact or {}, existing.format)
        return {
            "id": existing.id,
            "format": existing.format,
            "file_sha256": existing.file_sha256,
            "artifact": existing.artifact,
            "content": content,
            "media_type": media_type,
            "idempotent_replay": True,
        }
    research_case = db.get(ResearchCase, case_id)
    config = db.get(CapabilityConfig, run.config_id)
    template = db.get(CapabilityTemplate, config.template_id) if config else None
    artifact = {
        "format": payload.format,
        "project": {"id": case_id, "title": research_case.title, "scope": research_case.scope},
        "output": target.artifact_snapshot,
        "capability": {
            "catalog_key": template.catalog_key if template else None,
            "slug": template.slug if template else None,
            "version": template.version if template else None,
        },
        "run_input": run.input_snapshot,
        "confirmed_by": target.confirmed_by,
        "confirmed_at": _iso_datetime(target.confirmed_at),
    }
    content, media_type = _research_export_content(artifact, payload.format)
    encoded = content.encode("utf-8")
    export = ResearchExport(
        research_case_id=case_id,
        capability_run_target_id=target.id,
        requested_by=actor.id,
        format=payload.format,
        idempotency_key=payload.idempotency_key,
        frozen_scope=dict(research_case.scope or {}),
        manifest_snapshot=_skill_manifest_v2(template) if template else {},
        rights_snapshot={
            "raw_content_exported": False,
            "restricted_content_exported": False,
            "policy": "metadata-citation-allowed-summary-only",
        },
        file_sha256=hashlib.sha256(encoded).hexdigest(),
        artifact=artifact,
    )
    db.add(export)
    db.flush()
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="output_exported",
        object_type="research_export",
        object_key=export.id,
        details={"format": export.format, "target_id": target.id, "file_sha256": export.file_sha256},
    )
    db.commit()
    db.refresh(export)
    return {
        "id": export.id,
        "format": export.format,
        "file_sha256": export.file_sha256,
        "artifact": artifact,
        "content": content,
        "media_type": media_type,
        "idempotent_replay": False,
    }


_MATERIAL_CHANNELS = {
    "frontier_research",
    "think_tank_report",
    "policy",
    "policy_center",
    "data_aggregation",
    "event_dynamics",
    "other",
}

_FRONTIER_SOURCE_TYPES = {"academic", "journal", "academic_journal"}
_THINK_TANK_SOURCE_TYPES = {
    "thinktank",
    "think_tank",
    "research_institute",
    "research_organization",
    "policy_research",
    "研究机构",
}
_POLICY_DOCUMENT_TYPES = {"policy", "policy_document", "policy_brief"}
_POLICY_TITLE_MARKERS = ("circulaire", "arrêté", "decret", "décret", "règlement")


def _material_channel(document: Document, source: Source) -> str:
    document_type = (document.document_type or "").lower()
    source_type = (source.source_type or "").lower()
    source_homepage = (source.homepage_url or "").lower()
    title = (document.title or "").lower()
    if (
        document_type in {"journal_article", "working_paper"}
        or source_type in _FRONTIER_SOURCE_TYPES
        or "nature.com/articles" in source_homepage
    ):
        return "frontier_research"
    if document_type in _POLICY_DOCUMENT_TYPES:
        return "policy_center"
    if source_type in _THINK_TANK_SOURCE_TYPES:
        return "think_tank_report"
    if document_type == "report":
        return "think_tank_report"
    if source_type == "government_or_international" and any(
        marker in title for marker in _POLICY_TITLE_MARKERS
    ):
        return "policy_center"
    if document_type == "dataset" or source_type == "data":
        return "data_aggregation"
    if document_type == "news" or source_type in {"media", "news_media"}:
        return "event_dynamics"
    return "other"


def _material_rights(
    version: DocumentVersion,
    policy: SourceChannelPolicy | None,
) -> dict[str, Any]:
    if policy is None:
        return {
            "status": "review_required",
            "storage_scope": "unknown",
            "rag_scope": "unknown",
            "terms_state": "unknown",
            "reviewed_at": None,
            "abstract_display_allowed": False,
        }
    abstract_allowed = policy.storage_scope in {
        "official_abstract",
        "extracted_text",
        "full_content",
    }
    reviewed = policy.reviewed_at is not None and policy.terms_state != "unknown"
    return {
        "status": "reviewed" if reviewed else "review_required",
        "storage_scope": policy.storage_scope,
        "rag_scope": policy.rag_scope,
        "terms_state": policy.terms_state,
        "reviewed_at": policy.reviewed_at,
        "abstract_display_allowed": bool(version.abstract and abstract_allowed),
    }


@router.get("/search/materials")
def search_materials(
    db: DbSession,
    q: Annotated[str | None, Query(max_length=200)] = None,
    topic: Annotated[str | None, Query(max_length=100)] = None,
    country_iso3: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    channel: Annotated[str | None, Query()] = None,
    source_id: Annotated[int | None, Query(gt=0)] = None,
    language: Annotated[str | None, Query(max_length=32)] = None,
    source_type: Annotated[str | None, Query(max_length=80)] = None,
    authority_level: Annotated[str | None, Query(max_length=80)] = None,
    storage_scope: Annotated[str | None, Query(max_length=32)] = None,
    document_type: Annotated[str | None, Query(max_length=80)] = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_unlinked: bool = False,
) -> dict[str, Any]:
    return _search_materials(
        db,
        q=q,
        topic=topic,
        country_iso3=country_iso3,
        channel=channel,
        source_id=source_id,
        language=language,
        source_type=source_type,
        authority_level=authority_level,
        storage_scope=storage_scope,
        document_type=document_type,
        published_from=published_from,
        published_to=published_to,
        limit=limit,
        offset=offset,
        include_unlinked=include_unlinked,
    )


def _search_materials(
    db: DbSession,
    q: Annotated[str | None, Query(max_length=200)] = None,
    topic: Annotated[str | None, Query(max_length=100)] = None,
    country_iso3: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    channel: Annotated[str | None, Query()] = None,
    source_id: Annotated[int | None, Query(gt=0)] = None,
    language: Annotated[str | None, Query(max_length=32)] = None,
    source_type: Annotated[str | None, Query(max_length=80)] = None,
    authority_level: Annotated[str | None, Query(max_length=80)] = None,
    storage_scope: Annotated[str | None, Query(max_length=32)] = None,
    document_type: Annotated[str | None, Query(max_length=80)] = None,
    published_from: datetime | None = None,
    published_to: datetime | None = None,
    limit: int | None = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_unlinked: bool = False,
) -> dict[str, Any]:
    if channel is not None and channel not in _MATERIAL_CHANNELS:
        raise HTTPException(status_code=422, detail="unsupported material channel")

    confirmed_entity_document = (
        select(DocumentVersion.document_id)
        .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
        .where(DocumentEntity.review_status == "confirmed")
    )
    confirmed_event_document = (
        select(DocumentVersion.document_id)
        .join(EventMention, EventMention.document_version_id == DocumentVersion.id)
        .where(EventMention.review_status == "confirmed")
    )
    statement = (
        select(DocumentVersion, Document, Source, SourceChannelPolicy)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .outerjoin(DocumentAbstractState, DocumentAbstractState.document_id == Document.id)
        .outerjoin(SourceChannelPolicy, SourceChannelPolicy.channel_id == DocumentAbstractState.channel_id)
        .where(DocumentVersion.version_no == Document.latest_version_no)
    )
    if not include_unlinked:
        statement = statement.where(
            or_(
                Document.id.in_(confirmed_entity_document),
                Document.id.in_(confirmed_event_document),
            )
        )
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(
                DocumentVersion.title.ilike(pattern),
                DocumentVersion.abstract.ilike(pattern),
                Document.title.ilike(pattern),
                Source.name.ilike(pattern),
                Source.organization_name.ilike(pattern),
            )
        )
    if country_iso3:
        statement = statement.where(
            select(DocumentEntity.document_version_id)
            .join(DocumentVersion, DocumentVersion.id == DocumentEntity.document_version_id)
            .join(ResearchEntity, ResearchEntity.id == DocumentEntity.entity_id)
            .where(
                DocumentVersion.document_id == Document.id,
                DocumentEntity.review_status == "confirmed",
                ResearchEntity.entity_type == "country",
                ResearchEntity.canonical_key == country_iso3,
            )
            .exists()
        )
    if language:
        statement = statement.where(DocumentVersion.language == language)
    if source_type:
        statement = statement.where(Source.source_type == source_type)
    if authority_level:
        statement = statement.where(Source.authority_level == authority_level)
    if storage_scope:
        statement = statement.where(SourceChannelPolicy.storage_scope == storage_scope)
    if document_type:
        statement = statement.where(Document.document_type == document_type)
    if channel == "frontier_research":
        statement = statement.where(
            or_(
                Document.document_type.in_(("journal_article", "working_paper")),
                Source.source_type.in_(tuple(_FRONTIER_SOURCE_TYPES)),
                Source.homepage_url.ilike("%nature.com/articles%"),
            )
        )
    elif channel == "think_tank_report":
        statement = statement.where(
            or_(
                Document.document_type == "report",
                Source.source_type.in_(tuple(_THINK_TANK_SOURCE_TYPES)),
            )
        )
    elif channel in {"policy", "policy_center"}:
        statement = statement.where(
            or_(
                Document.document_type.in_(tuple(_POLICY_DOCUMENT_TYPES)),
                and_(
                    Source.source_type == "government_or_international",
                    or_(*(Document.title.ilike(f"%{marker}%") for marker in _POLICY_TITLE_MARKERS)),
                ),
            )
        )
    elif channel == "data_aggregation":
        statement = statement.where(or_(Document.document_type == "dataset", Source.source_type == "data"))
    elif channel == "event_dynamics":
        statement = statement.where(
            or_(Document.document_type == "news", Source.source_type.in_(("media", "news_media")))
        )

    effective_published_at = func.coalesce(DocumentVersion.published_at, Document.published_at)
    if published_from:
        statement = statement.where(effective_published_at >= published_from)
    if published_to:
        statement = statement.where(effective_published_at <= published_to)
    rows = list(
        db.execute(
            statement.order_by(
                effective_published_at.is_(None),
                effective_published_at.desc(),
                Document.last_seen_at.desc(),
                Document.id.desc(),
            )
        )
    )

    grouped_rows: dict[int, list[tuple[DocumentVersion, Document, Source, SourceChannelPolicy | None]]] = {}
    for row in rows:
        grouped_rows.setdefault(row[1].id, []).append(row)

    confirmed_version_ids = set(
        db.scalars(
            select(DocumentEntity.document_version_id).where(DocumentEntity.review_status == "confirmed")
        )
    ) | set(
        db.scalars(select(EventMention.document_version_id).where(EventMention.review_status == "confirmed"))
    )
    items: list[dict[str, Any]] = []
    for document_rows in grouped_rows.values():
        version, document, source, _ = document_rows[0]
        policies = {row[3].channel_id: row[3] for row in document_rows if row[3] is not None}
        policy = next(iter(policies.values())) if len(policies) == 1 else None
        material_channel = _material_channel(document, source)
        if channel == "other" and material_channel != "other":
            continue
        rights = _material_rights(version, policy)
        summary = _document_summary(version, document, source)
        items.append(
            {
                **summary,
                "abstract": version.abstract if rights["abstract_display_allowed"] else None,
                "authority_level": source.authority_level,
                "channel": material_channel,
                "evidence_status": (
                    "confirmed" if version.id in confirmed_version_ids else "source_catalogued"
                ),
                "rights": rights,
                "locator": {
                    "canonical_url": document.canonical_url,
                    "discovery_url": document.discovery_url,
                    "official_locator_ready": document.canonical_url is not None,
                },
            }
        )

    if topic:
        items = [item for item in items if topic in item.get("metadata", {}).get("topics", [])]
    source_counts: dict[int, dict[str, Any]] = {}
    for item in items:
        entry = source_counts.setdefault(
            item["source_id"],
            {
                "source_id": item["source_id"],
                "source_name": item["source_name"],
                "count": 0,
            },
        )
        entry["count"] += 1
    if source_id is not None:
        items = [item for item in items if item["source_id"] == source_id]
    total = len(items)
    page = items[offset : offset + limit if limit is not None else None]
    return {
        "items": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "facets": {
            "sources": list(source_counts.values()),
            "channels": sorted({item["channel"] for item in items}),
            "languages": sorted({item["language"] for item in items if item["language"]}),
            "source_types": sorted({item["source_type"] for item in items}),
            "document_types": sorted({item["document_type"] for item in items}),
            "storage_scopes": sorted({item["rights"]["storage_scope"] for item in items}),
        },
    }


def _search_date_bound(value: date | None, *, end: bool = False) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, datetime.max.time() if end else datetime.min.time(), tzinfo=UTC)


def _unified_material_type(item: dict[str, Any]) -> str:
    return {
        "frontier_research": "paper",
        "think_tank_report": "report",
        "policy_center": "policy",
        "data_aggregation": "data",
    }.get(item.get("channel"), "material")


@router.get("/search")
def unified_search(
    db: DbSession,
    q: Annotated[str | None, Query(max_length=200)] = None,
    object_type: Annotated[str, Query()] = "all",
    country_iso3: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    topic: Annotated[str | None, Query(max_length=160)] = None,
    actor: Annotated[str | None, Query(max_length=160)] = None,
    source_type: Annotated[str | None, Query(max_length=80)] = None,
    language: Annotated[str | None, Query(max_length=32)] = None,
    date_basis: Annotated[str, Query(pattern=r"^(published|occurred|updated)$")] = "published",
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    if object_type not in _UNIFIED_OBJECT_TYPES:
        raise HTTPException(status_code=422, detail="unsupported object type")
    if country_iso3:
        _require_catalog_country(country_iso3)
    start = _search_date_bound(date_from)
    end = _search_date_bound(date_to, end=True)
    items: list[dict[str, Any]] = []

    include_materials = object_type in {"all", "policy", "report", "paper"}
    if include_materials:
        channel = {
            "policy": "policy_center",
            "report": "think_tank_report",
            "paper": "frontier_research",
        }.get(object_type)
        materials = search_materials(
            db=db,
            q=q,
            country_iso3=country_iso3,
            channel=channel,
            language=language,
            source_type=source_type,
            authority_level=None,
            storage_scope=None,
            document_type=None,
            published_from=start if date_basis == "published" else None,
            published_to=end if date_basis == "published" else None,
            limit=500,
            offset=0,
            include_unlinked=True,
        )["items"]
        version_ids = [item["document_version_id"] for item in materials]
        allowed_versions = set(version_ids)
        if topic and version_ids:
            pattern = f"%{topic.strip()}%"
            allowed_versions &= set(
                db.scalars(
                    select(DocumentTopic.document_version_id)
                    .join(Topic, Topic.id == DocumentTopic.topic_id)
                    .where(
                        DocumentTopic.document_version_id.in_(version_ids),
                        DocumentTopic.review_status == "confirmed",
                        or_(Topic.title.ilike(pattern), Topic.description.ilike(pattern)),
                    )
                )
            )
        if actor and version_ids:
            pattern = f"%{actor.strip()}%"
            allowed_versions &= set(
                db.scalars(
                    select(DocumentEntity.document_version_id)
                    .join(ResearchEntity, ResearchEntity.id == DocumentEntity.entity_id)
                    .where(
                        DocumentEntity.document_version_id.in_(version_ids),
                        DocumentEntity.review_status == "confirmed",
                        ResearchEntity.entity_type.in_(("organization", "person", "armed_group", "policy")),
                        ResearchEntity.canonical_name.ilike(pattern),
                    )
                )
            )
        country_links: dict[int, list[dict[str, str]]] = defaultdict(list)
        if version_ids:
            for version_id, entity in db.execute(
                select(DocumentEntity.document_version_id, ResearchEntity)
                .join(ResearchEntity, ResearchEntity.id == DocumentEntity.entity_id)
                .where(
                    DocumentEntity.document_version_id.in_(version_ids),
                    DocumentEntity.review_status == "confirmed",
                    ResearchEntity.entity_type == "country",
                )
            ):
                country_links[version_id].append(
                    {"iso3": entity.canonical_key, "name": entity.canonical_name}
                )
        source_ids = {item["source_id"] for item in materials}
        sources = (
            {source.id: source for source in db.scalars(select(Source).where(Source.id.in_(source_ids)))}
            if source_ids
            else {}
        )
        for material in materials:
            if material["document_version_id"] not in allowed_versions:
                continue
            material_type = _unified_material_type(material)
            if object_type != "all" and material_type != object_type:
                continue
            if date_basis == "updated":
                changed_at = material.get("updated_at") or material.get("observed_at")
                if start and (changed_at is None or changed_at < start):
                    continue
                if end and (changed_at is None or changed_at > end):
                    continue
            rights = material["rights"]
            reviewed = rights.get("status") == "reviewed"
            locator = material.get("locator") or {}
            safe_locator = (
                {
                    "canonical_url": locator.get("canonical_url"),
                    "official_locator_ready": bool(locator.get("canonical_url")),
                    "page": material.get("metadata", {}).get("page"),
                    "pages": material.get("metadata", {}).get("pages"),
                    "paragraph": material.get("metadata", {}).get("paragraph"),
                }
                if reviewed and locator.get("canonical_url")
                else None
            )
            source = sources.get(material["source_id"])
            items.append(
                {
                    "object_type": material_type,
                    "object_key": f"document-version:{material['document_version_id']}",
                    "title": material["title"],
                    "subtitle": material.get("metadata", {}).get("citation") or material.get("document_type"),
                    "country": country_links.get(material["document_version_id"], []),
                    "source": {
                        "id": material["source_id"],
                        "name": material["source_name"],
                        "type": material["source_type"],
                        "profile": _source_profile(source) if source else None,
                    },
                    "language": material.get("language"),
                    "published_at": material.get("published_at"),
                    "occurred_at": None,
                    "recorded_at": material.get("recorded_at"),
                    "updated_at": material.get("updated_at") or material.get("observed_at"),
                    "version": f"v{material.get('version_no', 1)}",
                    "evidence_status": material["evidence_status"],
                    "access_state": "available" if reviewed else "review_required",
                    "rights": rights,
                    "locator": safe_locator,
                    "summary": material.get("abstract") if reviewed else None,
                    "metadata": material.get("metadata", {}),
                    "related_objects": country_links.get(material["document_version_id"], []),
                    "actions": {
                        "can_cite": True,
                        "can_export_metadata": True,
                        "can_open_source": bool(safe_locator),
                        "can_add_to_project": True,
                        "can_request_access": False,
                    },
                }
            )

    if object_type == "country" or (object_type == "all" and (q or country_iso3 or topic)):
        normalized_query = (q or "").strip().lower()
        topic_query = (topic or "").strip().lower()
        for country in get_country_catalog(db)["countries"]:
            if country_iso3 and country["iso3"] != country_iso3:
                continue
            if normalized_query and not any(
                normalized_query in str(value or "").lower()
                for value in (country["iso3"], country["name_zh"], country["name_en"])
            ):
                continue
            if topic_query and not any(
                topic_query in str(tag).lower() for tag in country["template"]["focus_tags"]
            ):
                continue
            items.append(
                {
                    "object_type": "country",
                    "object_key": f"country:{country['iso3']}",
                    "title": country["name_zh"],
                    "subtitle": country["name_en"],
                    "country": [{"iso3": country["iso3"], "name": country["name_zh"]}],
                    "source": {"name": "国家基础档案固定快照", "type": "reference_registry"},
                    "language": "zh",
                    "published_at": None,
                    "occurred_at": None,
                    "recorded_at": None,
                    "updated_at": country["last_evidence_at"],
                    "version": country.get("m49"),
                    "evidence_status": country["coverage_quality"],
                    "access_state": "available",
                    "rights": {"status": "reviewed", "license": "ODbL-1.0"},
                    "locator": {"route": f"#/countries/{country['iso3']}"},
                    "summary": country["template"]["profile_description"],
                    "metadata": {
                        "profile_completeness": country["profile_completeness"],
                        "gap_reasons": country["gap_reasons"],
                    },
                    "related_objects": [],
                    "actions": {
                        "can_cite": True,
                        "can_export_metadata": True,
                        "can_open_source": True,
                        "can_add_to_project": False,
                        "can_request_access": False,
                    },
                }
            )

    if object_type in {"all", "event"}:
        statement = (
            select(ResearchEvent, ResearchEntity)
            .join(ResearchEntity, ResearchEntity.id == ResearchEvent.country_entity_id)
            .where(ResearchEvent.review_status == "reviewed")
        )
        if country_iso3:
            statement = statement.where(ResearchEntity.canonical_key == country_iso3)
        if q:
            pattern = f"%{q.strip()}%"
            statement = statement.where(
                or_(ResearchEvent.title.ilike(pattern), ResearchEvent.summary.ilike(pattern))
            )
        if topic:
            pattern = f"%{topic.strip()}%"
            statement = statement.where(
                or_(ResearchEvent.title.ilike(pattern), ResearchEvent.summary.ilike(pattern))
            )
        if actor:
            pattern = f"%{actor.strip()}%"
            statement = statement.where(
                select(EventEntity.event_id)
                .join(ResearchEntity, ResearchEntity.id == EventEntity.entity_id)
                .where(
                    EventEntity.event_id == ResearchEvent.id,
                    EventEntity.role == "actor",
                    ResearchEntity.canonical_name.ilike(pattern),
                )
                .exists()
            )
        if source_type:
            statement = statement.where(
                select(EventMention.event_id)
                .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    EventMention.event_id == ResearchEvent.id,
                    EventMention.review_status == "confirmed",
                    Source.source_type == source_type,
                )
                .exists()
            )
        if language:
            statement = statement.where(
                select(EventMention.event_id)
                .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                .where(
                    EventMention.event_id == ResearchEvent.id,
                    EventMention.review_status == "confirmed",
                    DocumentVersion.language == language,
                )
                .exists()
            )
        event_date = ResearchEvent.updated_at if date_basis == "updated" else ResearchEvent.start_at
        if start:
            statement = statement.where(event_date >= start)
        if end:
            statement = statement.where(event_date <= end)
        event_rows = list(db.execute(statement.limit(100)))
        event_ids = [event.id for event, _ in event_rows]
        event_source_counts = (
            {
                event_id: int(count or 0)
                for event_id, count in db.execute(
                    select(EventMention.event_id, func.count(func.distinct(Document.source_id)))
                    .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        EventMention.event_id.in_(event_ids),
                        EventMention.review_status == "confirmed",
                    )
                    .group_by(EventMention.event_id)
                )
            }
            if event_ids
            else {}
        )
        items.extend(
            {
                "object_type": "event",
                "object_key": f"event:{event.id}",
                "title": event.title,
                "subtitle": event.event_type,
                "country": [{"iso3": country.canonical_key, "name": country.canonical_name}],
                "source": {
                    "name": f"{event_source_counts.get(event.id, 0)} 个已确认来源",
                    "type": "reviewed_evidence",
                },
                "language": None,
                "published_at": None,
                "occurred_at": event.start_at,
                "recorded_at": event.created_at,
                "updated_at": event.updated_at,
                "version": event.details.get("version") or "reviewed",
                "evidence_status": "reviewed",
                "access_state": "available",
                "rights": {"status": "reviewed", "scope": "metadata_and_confirmed_claims"},
                "locator": {"route": f"#/countries/{country.canonical_key}/events/{event.id}"},
                "summary": event.summary,
                "metadata": {
                    "series_key": event.series_key,
                    "date_precision": event.date_precision,
                    "source_count": event_source_counts.get(event.id, 0),
                },
                "related_objects": [],
                "actions": {
                    "can_cite": True,
                    "can_export_metadata": True,
                    "can_open_source": True,
                    "can_add_to_project": True,
                    "can_request_access": False,
                },
            }
            for event, country in event_rows
        )

        # Source-verified reports are a separate evidence layer from reviewed events.
        # They remain platform aggregation work and never require researcher review.
        if not actor:
            verified_reports = _drc_verified_harvest_reports(
                country_iso3=country_iso3,
                q=q,
                date_from=start if date_basis == "published" else None,
                date_to=end if date_basis == "published" else None,
            )
            for report in verified_reports:
                searchable = f"{report['title']} {report['source_name']}".casefold()
                if topic and topic.strip().casefold() not in searchable:
                    continue
                if source_type and report["source_type"] != source_type:
                    continue
                if language and report["language"] != language:
                    continue
                if date_basis == "occurred" and (start or end):
                    continue
                if date_basis == "updated":
                    changed_at = report.get("updated_at") or report.get("observed_at")
                    if start and (changed_at is None or changed_at < start):
                        continue
                    if end and (changed_at is None or changed_at > end):
                        continue
                url = str(report.get("canonical_url") or report.get("discovery_url") or "")
                items.append(
                    {
                        "object_type": "event",
                        "object_key": (
                            f"source-report:{report['source_key']}:"
                            f"{hashlib.sha256(url.encode()).hexdigest()[:16]}"
                        ),
                        "title": report["title"],
                        "subtitle": "来源已核验最新报道 · 待平台聚合",
                        "country": report["countries"],
                        "source": {
                            "name": report["source_name"],
                            "type": report["source_type"],
                            "key": report["source_key"],
                        },
                        "language": report["language"],
                        "published_at": report["published_at"],
                        "occurred_at": None,
                        "recorded_at": report["recorded_at"],
                        "updated_at": report["updated_at"],
                        "version": "source_metadata_snapshot",
                        "evidence_status": "source_verified",
                        "access_state": "source_available",
                        "rights": {
                            "status": "review_required",
                            "scope": "source_metadata_only",
                        },
                        "locator": {"canonical_url": url},
                        "summary": None,
                        "metadata": {
                            "aggregation_status": "pending_platform_aggregation",
                            "requires_researcher_review": False,
                        },
                        "related_objects": [],
                        "actions": {
                            "can_cite": False,
                            "can_export_metadata": True,
                            "can_open_source": bool(url),
                            "can_add_to_project": False,
                            "can_request_access": False,
                        },
                    }
                )

    if object_type in {"all", "data"} and not actor and not language:
        statement = (
            select(
                StructuredDataset,
                Source,
                StructuredObservation.country_iso3,
                func.count(StructuredObservation.id),
                func.max(StructuredObservation.last_seen_at),
            )
            .join(SourceChannel, SourceChannel.id == StructuredDataset.channel_id)
            .join(Source, Source.id == SourceChannel.source_id)
            .join(
                StructuredObservation,
                StructuredObservation.dataset_id == StructuredDataset.id,
            )
            .group_by(StructuredDataset.id, Source.id, StructuredObservation.country_iso3)
        )
        if country_iso3:
            statement = statement.where(StructuredObservation.country_iso3 == country_iso3)
        if source_type:
            statement = statement.where(Source.source_type == source_type)
        term = (q or topic or "").strip()
        if term:
            pattern = f"%{term}%"
            statement = statement.where(
                or_(StructuredDataset.name.ilike(pattern), StructuredDataset.dataset_key.ilike(pattern))
            )
        for dataset, source, iso3, count, updated_at in db.execute(statement.limit(100)):
            if start and (updated_at is None or updated_at < start):
                continue
            if end and (updated_at is None or updated_at > end):
                continue
            country_name = _country_name(iso3)
            items.append(
                {
                    "object_type": "data",
                    "object_key": f"dataset:{dataset.id}:{iso3}",
                    "title": dataset.name,
                    "subtitle": dataset.dataset_key,
                    "country": [{"iso3": iso3, "name": country_name}],
                    "source": {"id": source.id, "name": source.name, "type": source.source_type},
                    "language": None,
                    "published_at": None,
                    "occurred_at": None,
                    "recorded_at": None,
                    "updated_at": updated_at,
                    "version": "latest_snapshot",
                    "evidence_status": "snapshot_pinned",
                    "access_state": "metadata_available",
                    "rights": {
                        "status": "review_required",
                        "citation": "source_attribution_required",
                        "download": "review_required",
                    },
                    "locator": {"route": f"#/countries/{iso3}/data"},
                    "summary": f"{count} 条规范化观测；不包含原始响应。",
                    "metadata": {"observation_count": int(count or 0), "frequency": dataset.frequency},
                    "related_objects": [],
                    "actions": {
                        "can_cite": True,
                        "can_export_metadata": True,
                        "can_open_source": True,
                        "can_add_to_project": False,
                        "can_request_access": False,
                    },
                }
            )

    def sort_time(item: dict[str, Any]) -> datetime:
        if date_basis == "published":
            value = item.get("published_at") or item.get("occurred_at")
        elif date_basis == "occurred":
            value = item.get("occurred_at")
        else:
            value = item.get("updated_at") or item.get("published_at") or item.get("occurred_at")
        if value is None:
            return datetime.min.replace(tzinfo=UTC)
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    items.sort(key=sort_time, reverse=True)
    total = len(items)
    page = items[offset : offset + limit if limit is not None else None]
    return {
        "items": page,
        "facets": {
            "object_types": sorted({item["object_type"] for item in items}),
            "countries": sorted({country["iso3"] for item in items for country in item.get("country", [])}),
            "languages": sorted({item["language"] for item in items if item.get("language")}),
            "source_types": sorted(
                {item["source"].get("type") for item in items if item.get("source", {}).get("type")}
            ),
            "access_states": sorted({item["access_state"] for item in items}),
        },
        "total": total,
        "limit": limit,
        "offset": offset,
        "query": {
            "q": q,
            "object_type": object_type,
            "country_iso3": country_iso3,
            "topic": topic,
            "actor": actor,
            "source_type": source_type,
            "language": language,
            "date_basis": date_basis,
            "date_from": date_from,
            "date_to": date_to,
        },
    }


@router.get("/materials/{document_version_id}")
def get_material(document_version_id: int, db: DbSession) -> dict[str, Any]:
    row = db.execute(
        select(DocumentVersion, Document, Source)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(DocumentVersion.id == document_version_id)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="document version not found")
    version, document, source = row
    entity_rows = list(
        db.execute(
            select(DocumentEntity, ResearchEntity)
            .join(ResearchEntity, ResearchEntity.id == DocumentEntity.entity_id)
            .where(DocumentEntity.document_version_id == document_version_id)
            .order_by(ResearchEntity.entity_type, ResearchEntity.canonical_name)
        )
    )
    event_rows = list(
        db.execute(
            select(EventMention, ResearchEvent)
            .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
            .where(
                EventMention.document_version_id == document_version_id,
                EventMention.review_status == "confirmed",
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(ResearchEvent.start_at, ResearchEvent.id)
        )
    )
    topic_rows = list(
        db.execute(
            select(ResearchCaseDocument, ResearchCase, DocumentVersion)
            .join(ResearchCase, ResearchCase.id == ResearchCaseDocument.research_case_id)
            .join(DocumentVersion, DocumentVersion.id == ResearchCaseDocument.document_version_id)
            .where(DocumentVersion.document_id == document.id)
            .order_by(ResearchCase.title)
        )
    )
    topics_by_case: dict[int, dict[str, Any]] = {}
    for link, research_case, linked_version in topic_rows:
        item = topics_by_case.setdefault(
            research_case.id,
            {
                "id": research_case.id,
                "title": research_case.title,
                "usage_type": link.usage_type,
                "linked_version_ids": [],
            },
        )
        item["linked_version_ids"].append(linked_version.id)
    confirmed_entities = [link for link, _ in entity_rows if link.review_status == "confirmed"]
    locators = [mention.evidence_locator for mention, _ in event_rows if mention.evidence_locator]
    abstract_state = db.get(DocumentAbstractState, document.id)
    policy = (
        db.get(SourceChannelPolicy, abstract_state.channel_id)
        if abstract_state and abstract_state.channel_id
        else None
    )
    rights = _material_rights(version, policy)
    return {
        **_document_summary(version, document, source),
        "abstract": version.abstract if rights["abstract_display_allowed"] else None,
        "rights": rights,
        "content_sha256": version.content_sha256,
        "review_status": ("confirmed" if confirmed_entities or event_rows else "pending"),
        "evidence_locator": (
            json.dumps(locators[0], ensure_ascii=False) if len(locators) == 1 else f"{len(locators)} 个定位"
        )
        if locators
        else None,
        "evidence_locators": locators,
        "countries": [
            {"iso3": entity.canonical_key, "name": entity.canonical_name, "role": link.role}
            for link, entity in entity_rows
            if entity.entity_type == "country" and link.review_status == "confirmed"
        ],
        "events": [
            {"id": event.id, "title": event.title, "event_key": event.event_key} for _, event in event_rows
        ],
        "topics": list(topics_by_case.values()),
    }


@router.get("/events")
def list_events(
    db: DbSession,
    country_iso3: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    event_type: Annotated[str | None, Query(pattern=r"^(policy|conflict|market|accident|other)$")] = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    source_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    statement = (
        select(ResearchEvent)
        .where(ResearchEvent.review_status == "reviewed")
        .order_by(
            ResearchEvent.start_at.is_(None),
            ResearchEvent.start_at.desc(),
            ResearchEvent.id.desc(),
        )
    )
    if country_iso3:
        country_id = db.scalar(
            select(ResearchEntity.id).where(
                ResearchEntity.entity_type == "country",
                ResearchEntity.canonical_key == country_iso3,
            )
        )
        if country_id is None:
            return []
        statement = statement.where(ResearchEvent.country_entity_id == country_id)
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(ResearchEvent.title.ilike(pattern), ResearchEvent.summary.ilike(pattern))
        )
    if event_type:
        statement = statement.where(ResearchEvent.event_type == event_type)
    if date_from:
        statement = statement.where(ResearchEvent.start_at >= date_from)
    if date_to:
        statement = statement.where(ResearchEvent.start_at <= date_to)
    if source_id:
        statement = statement.where(
            select(EventMention.id)
            .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                EventMention.event_id == ResearchEvent.id,
                EventMention.review_status == "confirmed",
                Document.source_id == source_id,
            )
            .exists()
        )
    return [
        {**_event_summary(event), "source_count": _event_source_count(db, event.id)}
        for event in db.scalars(statement.limit(limit))
    ]


@router.get("/event-reports")
def list_event_reports(
    db: DbSession,
    country_iso3: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    q: Annotated[str | None, Query(min_length=1, max_length=160)] = None,
    event_type: Annotated[str | None, Query(pattern=r"^(policy|conflict|market|accident|other)$")] = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    source_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    statement = (
        select(DocumentVersion, Document, Source, ResearchEntity)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .join(DocumentEntity, DocumentEntity.document_version_id == DocumentVersion.id)
        .join(ResearchEntity, ResearchEntity.id == DocumentEntity.entity_id)
        .where(
            DocumentVersion.version_no == Document.latest_version_no,
            DocumentEntity.review_status == "confirmed",
            ResearchEntity.entity_type == "country",
        )
        .order_by(
            func.coalesce(DocumentVersion.published_at, Document.published_at).is_(None),
            func.coalesce(DocumentVersion.published_at, Document.published_at).desc(),
            Document.last_seen_at.desc(),
            Document.id.desc(),
        )
    )
    if get_settings().public_demo_enabled:
        statement = statement.where(
            or_(
                Document.document_type.in_(["news", "news_article"]),
                Source.source_type.in_(["news_media", "media"]),
            )
        )
    if country_iso3:
        _require_catalog_country(country_iso3)
        statement = statement.where(ResearchEntity.canonical_key == country_iso3)
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(
            or_(DocumentVersion.title.ilike(pattern), DocumentVersion.abstract.ilike(pattern))
        )
    effective_date = func.coalesce(DocumentVersion.published_at, Document.published_at)
    if date_from:
        statement = statement.where(effective_date >= date_from)
    if date_to:
        statement = statement.where(effective_date <= date_to)
    if source_id:
        statement = statement.where(Source.id == source_id)
    if event_type:
        statement = statement.where(
            select(EventMention.id)
            .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
            .where(
                EventMention.document_version_id == DocumentVersion.id,
                EventMention.review_status == "confirmed",
                ResearchEvent.review_status == "reviewed",
                ResearchEvent.event_type == event_type,
            )
            .exists()
        )

    grouped: dict[int, dict[str, Any]] = {}
    for version, document, source, country in db.execute(statement.limit(limit * 4)):
        report = grouped.setdefault(
            version.id,
            {
                **_document_summary(version, document, source),
                "review_status": "confirmed",
                "feed_status": "reported",
                "aggregation_status": "pending_aggregation",
                "countries": [],
                "linked_events": [],
            },
        )
        if not any(item["iso3"] == country.canonical_key for item in report["countries"]):
            report["countries"].append({"iso3": country.canonical_key, "name": country.canonical_name})

    version_ids = list(grouped)
    if version_ids:
        for version_id, event in db.execute(
            select(EventMention.document_version_id, ResearchEvent)
            .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
            .where(
                EventMention.document_version_id.in_(version_ids),
                EventMention.review_status == "confirmed",
                ResearchEvent.review_status == "reviewed",
            )
            .order_by(ResearchEvent.start_at.desc(), ResearchEvent.id.desc())
        ):
            grouped[version_id]["linked_events"].append(_event_summary(event))
            grouped[version_id]["aggregation_status"] = "linked_to_reviewed_event"
    reports = list(grouped.values())
    seen_urls = {
        str(report.get("canonical_url") or report.get("discovery_url") or "").strip() for report in reports
    }
    if not get_settings().public_demo_enabled and event_type is None and source_id is None:
        reports.extend(
            report
            for report in _drc_verified_harvest_reports(
                country_iso3=country_iso3,
                q=q,
                date_from=date_from,
                date_to=date_to,
            )
            if str(report.get("canonical_url") or report.get("discovery_url") or "").strip() not in seen_urls
        )
    reports.sort(
        key=lambda item: (
            item.get("published_at") is not None,
            _report_sort_value(item.get("published_at") or item.get("updated_at") or item.get("observed_at")),
        ),
        reverse=True,
    )
    return reports[:limit]


@router.get("/events/{event_id}")
def get_event(event_id: int, db: DbSession) -> dict[str, Any]:
    event = db.get(ResearchEvent, event_id)
    if event is None or event.review_status != "reviewed":
        raise HTTPException(status_code=404, detail="event not found")
    country = db.get(ResearchEntity, event.country_entity_id)
    entities = list(
        db.execute(
            select(EventEntity, ResearchEntity)
            .join(ResearchEntity, ResearchEntity.id == EventEntity.entity_id)
            .where(EventEntity.event_id == event_id)
            .order_by(EventEntity.role, ResearchEntity.canonical_name)
        )
    )
    relations = list(
        db.execute(
            select(EventRelation, ResearchEvent)
            .join(
                ResearchEvent,
                or_(
                    ResearchEvent.id == EventRelation.source_event_id,
                    ResearchEvent.id == EventRelation.target_event_id,
                ),
            )
            .where(
                or_(
                    EventRelation.source_event_id == event_id,
                    EventRelation.target_event_id == event_id,
                ),
                ResearchEvent.id != event_id,
            )
            .order_by(EventRelation.id)
        )
    )
    mention_count = db.scalar(
        select(func.count()).select_from(EventMention).where(EventMention.event_id == event_id)
    )
    series_events = [event]
    if event.series_key:
        series_events = list(
            db.scalars(
                select(ResearchEvent)
                .where(
                    ResearchEvent.series_key == event.series_key,
                    ResearchEvent.review_status == "reviewed",
                )
                .order_by(
                    ResearchEvent.start_at.is_(None),
                    ResearchEvent.start_at,
                    ResearchEvent.id,
                )
            )
        )
    return {
        **_event_summary(event),
        "country": {
            "id": country.id,
            "iso3": country.canonical_key,
            "name": country.canonical_name,
        }
        if country
        else None,
        "summary": event.summary,
        "date_precision": event.date_precision,
        "review_status": event.review_status,
        "details": event.details,
        "mention_count": mention_count or 0,
        "entities": [
            {
                "id": entity.id,
                "type": entity.entity_type,
                "name": entity.canonical_name,
                "role": link.role,
            }
            for link, entity in entities
        ],
        "relations": [
            {
                "relation_type": relation.relation_type,
                "direction": "outgoing" if relation.source_event_id == event_id else "incoming",
                "event": _event_summary(other_event),
                "note": relation.note,
            }
            for relation, other_event in relations
        ],
        "series_timeline": [
            {
                **_event_summary(item),
                "current": item.id == event_id,
                "source_count": _event_source_count(db, item.id),
            }
            for item in series_events
        ],
        "series_method_note": "同系列事件仅按登记时间排序；未登记关系时不推断因果。",
    }


@router.get("/events/{event_id}/evidence")
def get_event_evidence(event_id: int, db: DbSession) -> dict[str, Any]:
    event = db.get(ResearchEvent, event_id)
    if event is None or event.review_status != "reviewed":
        raise HTTPException(status_code=404, detail="event not found")
    effective_source_time = func.coalesce(
        EventMention.source_reported_start_at,
        DocumentVersion.published_at,
        Document.published_at,
    )
    rows = list(
        db.execute(
            select(EventMention, DocumentVersion, Document, Source)
            .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(EventMention.event_id == event_id, EventMention.review_status == "confirmed")
            .order_by(
                effective_source_time.is_(None),
                effective_source_time,
                Source.name,
                DocumentVersion.id,
            )
        )
    )
    claims = list(
        db.scalars(
            select(EvidenceClaim)
            .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
            .where(
                EventMention.event_id == event_id,
                EvidenceClaim.review_status == "confirmed",
            )
            .order_by(EvidenceClaim.comparison_key, EvidenceClaim.id)
        )
    )
    by_mention: dict[int, list[EvidenceClaim]] = defaultdict(list)
    by_comparison: dict[str, list[EvidenceClaim]] = defaultdict(list)
    for claim in claims:
        by_mention[claim.event_mention_id].append(claim)
        by_comparison[claim.comparison_key].append(claim)
    sources = [
        {
            "mention_id": mention.id,
            "review_status": mention.review_status,
            "match_score": _number(mention.match_score),
            "mention_summary": mention.mention_summary,
            "source_reported_start_at": mention.source_reported_start_at,
            "source_reported_place": mention.source_reported_place,
            "perspective_group": (
                mention.source_fields.get("perspective_group")
                if mention.source_fields.get("perspective_group") in _MANUAL_PERSPECTIVE_GROUPS
                else "unclassified"
            ),
            "perspective_method": (
                "manual_registered"
                if mention.source_fields.get("perspective_group") in _MANUAL_PERSPECTIVE_GROUPS
                else "unclassified_no_inference"
            ),
            "times": {
                "occurred_at": event.start_at,
                "reported_at": mention.source_reported_start_at
                or version.published_at
                or document.published_at,
                "recorded_at": mention.created_at,
                "updated_at": _latest_value(
                    mention.updated_at,
                    version.source_updated_at,
                    document.last_seen_at,
                ),
            },
            "evidence_locator": mention.evidence_locator,
            "document": _document_summary(version, document, source),
            "claims": [_claim_read(claim) for claim in by_mention[mention.id]],
        }
        for mention, version, document, source in rows
    ]
    return {
        "event": _event_summary(event),
        "source_count": len({row[3].id for row in rows}),
        "sources": sources,
        "comparison_groups": [
            {
                "comparison_key": key,
                "claims": [_claim_read(claim) for claim in group],
                "has_difference": len(
                    {(claim.value_text, _number(claim.numeric_value), claim.unit) for claim in group}
                )
                > 1,
            }
            for key, group in by_comparison.items()
        ],
        "perspective_groups": [
            {
                "key": key,
                "source_count": len(
                    {
                        item[3].id
                        for item in rows
                        if (
                            item[0].source_fields.get("perspective_group")
                            if item[0].source_fields.get("perspective_group") in _MANUAL_PERSPECTIVE_GROUPS
                            else "unclassified"
                        )
                        == key
                    }
                ),
            }
            for key in sorted(
                {
                    mention.source_fields.get("perspective_group")
                    if mention.source_fields.get("perspective_group") in _MANUAL_PERSPECTIVE_GROUPS
                    else "unclassified"
                    for mention, _, _, _ in rows
                }
            )
        ],
        "deduplication": {
            "basis": "document_version_and_source",
            "duplicate_reporting_is_not_risk": True,
            "method_note": "来源量按固定文档版本与来源去重；异常峰值只表示报道变化。",
        },
        "method_note": "来源主张并列展示；系统不取平均、不判定真伪或权威性。",
    }


_SKILL_CATEGORIES = {"platform_vertical", "general_research", "personal", "co_build"}
_SKILL_SCOPES = {"country", "event", "topic", "capability", "document"}
_SKILL_WRITEBACK_TARGETS = {"country", "event", "research_case", "document_version"}
_SKILL_FORBIDDEN_KEYS = {
    "code",
    "python",
    "script",
    "command",
    "executable",
    "binary",
    "package",
    "shell",
    "entrypoint",
    "external_tool",
}


class SkillManifestV2Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_code: str | None = Field(default=None, pattern=r"^S(?:0[1-9]|[1-9][0-9])$")
    catalog_key: str | None = Field(default=None, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160)
    category: str
    source_channel: str = Field(min_length=2, max_length=120)
    localization_sources: list[str] = Field(default_factory=list, max_length=20)
    supported_scopes: list[str] = Field(min_length=1, max_length=5)
    purpose: str = Field(min_length=4, max_length=1000)
    steps: list[str] = Field(min_length=1, max_length=20)
    human_checkpoints: list[str] = Field(min_length=1, max_length=20)
    writeback_targets: list[str] = Field(default_factory=list, max_length=4)
    permissions: list[str] = Field(default_factory=list, max_length=20)
    evidence_policy: str = Field(min_length=4, max_length=1000)
    cost_risk: dict[str, Any] = Field(default_factory=dict)
    when_to_use: list[str] = Field(default_factory=list, max_length=20)
    starter_prompt: str = Field(default="", max_length=1000)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_contract(self) -> SkillManifestV2Payload:
        if bool(self.skill_code) == bool(self.catalog_key):
            raise ValueError("exactly one of skill_code or catalog_key is required")
        if self.category not in _SKILL_CATEGORIES:
            raise ValueError("unsupported Skill category")
        if not set(self.supported_scopes) <= _SKILL_SCOPES:
            raise ValueError("unsupported Skill scope")
        if not set(self.writeback_targets) <= _SKILL_WRITEBACK_TARGETS:
            raise ValueError("unsupported Skill writeback target")
        lowered_permissions = " ".join(self.permissions).lower()
        if any(term in lowered_permissions for term in ("execute", "shell", "python", "script")):
            raise ValueError("arbitrary tool execution is not allowed")
        return self


class SkillDraftPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=160)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    name: str = Field(min_length=2, max_length=160)
    description: str = Field(min_length=4, max_length=1000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    manifest: SkillManifestV2Payload

    @model_validator(mode="after")
    def validate_declarative_schema(self) -> SkillDraftPayload:
        for schema in (self.input_schema, self.output_schema):
            if _contains_forbidden_skill_key(schema):
                raise ValueError("Skill upload only accepts a declarative JSON contract")
        return self


class CapabilityConfigPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=160)
    config: dict[str, Any] = Field(default_factory=dict)


class CapabilityConfigStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern=r"^(active|pending_review|paused|archived)$")


class CapabilityPreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_case_id: int | None = Field(default=None, gt=0)
    input_overrides: dict[str, Any] = Field(default_factory=dict)


class CapabilitySchedulePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: str = Field(pattern=r"^(daily|weekly|monthly)$")
    timezone: str = Field(default="Asia/Shanghai", min_length=3, max_length=64)
    next_run_at: datetime
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,64}$")


class CapabilityScheduleStatusPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern=r"^(active|paused|archived)$")


class CapabilityReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: str = Field(pattern=r"^(capability_template|capability_config|capability_run)$")
    target_id: int = Field(gt=0)
    review_type: str = Field(pattern=r"^(validation|team_publish|public_publish|run)$")
    status: str = Field(pattern=r"^(approved|rejected)$")
    checklist: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=2000)


class CapabilityRunWritebackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: str = Field(pattern=r"^(country|event|research_case|document_version)$")
    target_key: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,64}$")
    revision_id: int | None = Field(default=None, gt=0)


def _contains_forbidden_skill_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _SKILL_FORBIDDEN_KEYS or _contains_forbidden_skill_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_skill_key(item) for item in value)
    return False


def _skill_manifest_v2(template: CapabilityTemplate) -> dict[str, Any]:
    manifest = dict(template.default_config.get("skill") or {})
    legacy_code = {
        "country-brief": "S01",
        "policy-dynamics": "S03",
        "event-timeline": "S04",
        "field-material-organizer": "S05",
        "material-relevance-ranking": "S07",
        "contradictory-evidence-context": "S08",
        "country-comparison": "S09",
        "policy-impact-graph": "S10",
    }.get(template.slug)
    return {
        "manifest_version": "2.0",
        "skill_code": manifest.get("skill_code") or legacy_code,
        "catalog_key": manifest.get("catalog_key") or template.catalog_key,
        "category": manifest.get("category", "platform_vertical"),
        "source_channel": manifest.get("source_channel", "平台自研"),
        "localization_sources": manifest.get("localization_sources", []),
        "supported_scopes": manifest.get("supported_scopes", []),
        "purpose": manifest.get("purpose", template.description),
        "steps": manifest.get("steps", []),
        "human_checkpoints": manifest.get("human_checkpoints", ["运行前预览输入", "写回前人工确认"]),
        "writeback_targets": manifest.get("writeback_targets", []),
        "permissions": manifest.get("permissions", manifest.get("tool_permissions", [])),
        "evidence_policy": manifest.get("evidence_policy", "只使用当前作用域内可追溯证据。"),
        "cost_risk": manifest.get("cost_risk", {"cost": "按需运行", "risk": "证据不足时不生成正式产物"}),
        "when_to_use": manifest.get("when_to_use", []),
        "starter_prompt": manifest.get("starter_prompt", ""),
        "provenance": manifest.get("provenance", {}),
    }


def _capability_prerequisites(
    template: CapabilityTemplate,
    config: CapabilityConfig,
    db: Session,
) -> tuple[list[str], bool]:
    def input_present(value: Any) -> bool:
        return value is not None and value != "" and value != []

    missing: list[str] = []
    for field_name in template.input_schema.get("required") or []:
        value = config.config.get(field_name)
        if not input_present(value):
            missing.append(field_name)
            continue
        if field_name == "research_case_id" and db.get(ResearchCase, value) is None:
            missing.append(field_name)
    for field_group in template.input_schema.get("required_any_of") or []:
        available = any(input_present(config.config.get(field_name)) for field_name in field_group)
        if not available:
            missing.append(" / ".join(field_group))
    return missing, not missing


def _capability_runtime_readiness(
    template: CapabilityTemplate,
    config: CapabilityConfig,
    db: Session,
) -> dict[str, Any]:
    missing, prerequisites_ready = _capability_prerequisites(template, config, db)
    execution_allowlisted = (template.execution_plan or {}).get("mode") in {
        "server_allowlist",
        "internal_compatibility",
    }
    can_run = (
        prerequisites_ready
        and execution_allowlisted
        and config.status in {"active", "published"}
        and template.status == "active"
        and template.validation_status == "verified"
    )
    latest_revision = db.scalar(
        select(CapabilityConfigRevision)
        .where(CapabilityConfigRevision.config_id == config.id)
        .order_by(CapabilityConfigRevision.revision_no.desc())
        .limit(1)
    )
    verified_run = None
    if latest_revision is not None:
        verified_run = db.scalar(
            select(CapabilityRun)
            .where(
                CapabilityRun.config_id == config.id,
                CapabilityRun.config_revision_id == latest_revision.id,
                CapabilityRun.status == "succeeded",
            )
            .order_by(CapabilityRun.id.desc())
            .limit(1)
        )
    verified_usable = bool(can_run and verified_run is not None and verified_run.output)
    return {
        "can_run": can_run,
        "verified_usable": verified_usable,
        "availability_status": (
            "verified_usable" if verified_usable else "ready_unverified" if can_run else "not_ready"
        ),
        "missing_prerequisites": missing,
        "verified_run": verified_run,
    }


def _capability_config_visible(
    db: Session, config: CapabilityConfig, actor: User | None, permission: str = "view"
) -> bool:
    if actor is None:
        return False
    if config.scope_type == "personal":
        return config.owner_id == actor.id
    if config.research_case_id is None:
        return False
    try:
        require_case_permission(db, config.research_case_id, actor, permission)
    except ReaderAccessDenied:
        return False
    return True


def _require_capability_run_access(
    db: Session,
    run: CapabilityRun,
    actor: User,
    permission: str = "view",
) -> CapabilityConfig:
    config = db.get(CapabilityConfig, run.config_id)
    if config is None or not _capability_config_visible(db, config, actor, permission):
        raise HTTPException(status_code=403, detail="capability run is outside the current user's scope")
    from app.services import field_access

    try:
        field_access.enforce_run_access(
            db, run, actor, {"view": "read", "run_skill": "ai", "confirm": "cite"}.get(permission, permission)
        )
    except field_access.FieldAccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    return config


def _public_case_scope(scope: dict[str, Any] | None) -> dict[str, Any]:
    """Keep workflow metadata out of the legacy public scope shape."""
    return {
        key: value
        for key, value in (scope or {}).items()
        if key not in {"scope_revision", "context_bindings"}
    }


def _catalog_capability_payload(template: CapabilityTemplate) -> dict[str, Any]:
    acceptance = dict((template.execution_plan or {}).get("acceptance") or {})
    missing = (
        []
        if template.validation_status == "verified"
        else [
            "专家验收未通过" if template.validation_status == "failed" else "完整输入与证据定位验收",
            "权利与模型策略复核",
            "人工写回门禁回放",
        ]
    )
    missing = list(dict.fromkeys([*missing, *acceptance.get("pending", [])]))
    manifest = _skill_manifest_v2(template)
    manifest["catalog_key"] = template.catalog_key
    return {
        "manifest_version": manifest.get("manifest_version", "2.0"),
        "catalog_key": template.catalog_key,
        "skill_code": manifest.get("skill_code"),
        "requirement_contract": template.default_config.get("requirement_contract", {}),
        "name": template.name,
        "display_name": _material_capability_name(template),
        "description": template.description,
        "category": template.category,
        "version": template.version,
        "maintainer": "国别智枢平台",
        "visibility": template.visibility,
        "validation_status": template.validation_status,
        "maintenance_status": "maintained" if template.status == "active" else template.status,
        "supported_scopes": manifest.get("supported_scopes", []),
        "permissions": manifest.get("permissions", ["read_scoped_reviewed_evidence"]),
        "cost_risk": manifest.get("cost_risk", {}),
        "input_schema": template.input_schema,
        "output_schema": template.output_schema,
        "human_checkpoints": manifest.get("human_checkpoints", []),
        "writeback_targets": manifest.get("writeback_targets", []),
        "evidence_policy": manifest.get("evidence_policy"),
        "execution_mode": (template.execution_plan or {}).get("mode"),
        "validation_result": acceptance,
        "missing_acceptance": missing,
        "can_copy": True,
        "can_run": template.status == "active" and template.validation_status == "verified",
    }


def _material_capability_name(template):
    return template.name


@router.get("/capabilities/catalog")
def list_public_capability_catalog(
    db: DbSession,
    category: Annotated[str | None, Query(max_length=32)] = None,
    validation_status: Annotated[str | None, Query(max_length=16)] = None,
    scope: Annotated[str | None, Query(max_length=32)] = None,
    q: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    statement = select(CapabilityTemplate).where(
        CapabilityTemplate.visibility == "public",
        CapabilityTemplate.status != "retired",
        CapabilityTemplate.catalog_key.is_not(None),
    )
    if category:
        statement = statement.where(CapabilityTemplate.category == category)
    if validation_status:
        statement = statement.where(CapabilityTemplate.validation_status == validation_status)
    rows = list(db.scalars(statement.order_by(CapabilityTemplate.category, CapabilityTemplate.catalog_key)))
    payloads = sorted(
        [_catalog_capability_payload(item) for item in rows],
        key=lambda item: item.get("skill_code") or item["catalog_key"],
    )
    if scope:
        payloads = [item for item in payloads if scope in item["supported_scopes"]]
    if q:
        needle = q.strip().lower()
        payloads = [
            item
            for item in payloads
            if needle in f"{item['name']} {item['description']} {item['catalog_key']}".lower()
        ]
    return {
        "items": payloads,
        "count": len(payloads),
        "verified_count": sum(item["validation_status"] == "verified" for item in payloads),
        "catalog_version": _public_catalog_version(),
    }


def _public_catalog_version() -> str:
    from app.services.research_capabilities import PUBLIC_CAPABILITY_CATALOG_PATH

    return json.loads(PUBLIC_CAPABILITY_CATALOG_PATH.read_text(encoding="utf-8"))["catalog_version"]


@router.get("/capabilities/catalog/{catalog_key}")
def get_public_capability_catalog_item(catalog_key: str, db: DbSession) -> dict[str, Any]:
    template = db.scalar(
        select(CapabilityTemplate).where(
            CapabilityTemplate.catalog_key == catalog_key,
            CapabilityTemplate.visibility == "public",
            CapabilityTemplate.status != "retired",
        )
    )
    if template is None:
        raise HTTPException(status_code=404, detail="public capability not found")
    return _catalog_capability_payload(template)


class CapabilityCopyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=160)
    scope_type: str = Field(default="personal", pattern=r"^(personal|research_case)$")
    research_case_id: int | None = Field(default=None, gt=0)
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope(self) -> CapabilityCopyPayload:
        if self.scope_type == "research_case" and self.research_case_id is None:
            raise ValueError("research_case_id is required for project copies")
        if self.scope_type == "personal" and self.research_case_id is not None:
            raise ValueError("personal copies cannot bind a project")
        return self


def _create_capability_config_revision(
    db: Session, config: CapabilityConfig, template: CapabilityTemplate, actor_id: int
) -> CapabilityConfigRevision:
    revision_no = (
        db.scalar(
            select(func.max(CapabilityConfigRevision.revision_no)).where(
                CapabilityConfigRevision.config_id == config.id
            )
        )
        or 0
    ) + 1
    revision = CapabilityConfigRevision(
        config_id=config.id,
        revision_no=revision_no,
        name=config.name,
        config_snapshot=dict(config.config or {}),
        template_snapshot={
            "template_id": template.id,
            "catalog_key": template.catalog_key,
            "slug": template.slug,
            "version": template.version,
            "name": template.name,
            "display_name": _material_capability_name(template),
            "execution_plan": template.execution_plan,
        },
        created_by=actor_id,
    )
    db.add(revision)
    db.flush()
    return revision


@router.post(
    "/capabilities/catalog/{catalog_key}/copies",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def copy_public_capability(
    catalog_key: str,
    payload: CapabilityCopyPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    template = db.scalar(
        select(CapabilityTemplate).where(
            CapabilityTemplate.catalog_key == catalog_key,
            CapabilityTemplate.visibility == "public",
            CapabilityTemplate.status == "active",
        )
    )
    if template is None:
        raise HTTPException(status_code=404, detail="public capability not found")
    if payload.research_case_id:
        _case_permission(db, payload.research_case_id, actor, "edit")
    duplicate = select(CapabilityConfig.id).where(
        CapabilityConfig.name == payload.name,
        CapabilityConfig.scope_type == payload.scope_type,
    )
    duplicate = duplicate.where(
        CapabilityConfig.owner_id == actor.id
        if payload.scope_type == "personal"
        else CapabilityConfig.research_case_id == payload.research_case_id
    )
    if db.scalar(duplicate):
        raise HTTPException(status_code=409, detail="configuration name already exists in this scope")
    config = CapabilityConfig(
        template_id=template.id,
        owner_id=actor.id,
        scope_type=payload.scope_type,
        research_case_id=payload.research_case_id,
        name=payload.name,
        config=_validated_capability_config(template, payload.config),
        status="draft" if template.validation_status != "verified" else "active",
    )
    if (
        template.catalog_key in {"research-s03", "research-s05"}
        and _capability_prerequisites(template, config, db)[0]
    ):
        config.status = "draft"
    db.add(config)
    db.flush()
    revision = _create_capability_config_revision(db, config, template, actor.id)
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="copied",
            target_type="capability_config",
            target_id=config.id,
            details={"catalog_key": catalog_key, "revision_id": revision.id, "scope_type": config.scope_type},
        )
    )
    db.commit()
    return {
        "id": config.id,
        "name": config.name,
        "scope_type": config.scope_type,
        "research_case_id": config.research_case_id,
        "status": config.status,
        "revision": revision.revision_no,
        "can_run": template.validation_status == "verified" and config.status == "active",
    }


@router.get("/capability-configs")
def list_current_capability_configs(db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    rows = [
        item
        for item in db.scalars(
            select(CapabilityConfig)
            .where(CapabilityConfig.status != "archived")
            .order_by(CapabilityConfig.updated_at.desc())
        )
        if _capability_config_visible(db, item, actor)
    ]
    items = []
    for item in rows:
        template = db.get(CapabilityTemplate, item.template_id)
        if template is None:
            continue
        latest_run = db.scalar(
            select(CapabilityRun)
            .where(CapabilityRun.config_id == item.id)
            .order_by(CapabilityRun.id.desc())
            .limit(1)
        )
        readiness = _capability_runtime_readiness(template, item, db)
        items.append(
            {
                "id": item.id,
                "name": item.name,
                "scope_type": item.scope_type,
                "research_case_id": item.research_case_id,
                "status": item.status,
                "template_id": item.template_id,
                "catalog_key": template.catalog_key,
                "description": template.description,
                "category": template.category,
                "version": template.version,
                "validation_status": template.validation_status,
                "supported_scopes": _skill_manifest_v2(template).get("supported_scopes") or [],
                "input_schema": template.input_schema,
                "output_schema": template.output_schema,
                "can_run": readiness["can_run"],
                "verified_usable": readiness["verified_usable"],
                "availability_status": readiness["availability_status"],
                "missing_prerequisites": readiness["missing_prerequisites"],
                "verified_run": (
                    _run_summary(readiness["verified_run"]) if readiness["verified_run"] is not None else None
                ),
                "latest_run": _run_summary(latest_run) if latest_run else None,
                "updated_at": item.updated_at,
            }
        )
    return {"items": items, "count": len(items)}


def _capability_config_detail(db: Session, config: CapabilityConfig) -> dict[str, Any]:
    template = db.get(CapabilityTemplate, config.template_id)
    latest_revision = db.scalar(
        select(CapabilityConfigRevision)
        .where(CapabilityConfigRevision.config_id == config.id)
        .order_by(CapabilityConfigRevision.revision_no.desc())
        .limit(1)
    )
    run_count = (
        db.scalar(select(func.count(CapabilityRun.id)).where(CapabilityRun.config_id == config.id)) or 0
    )
    latest_run = db.scalar(
        select(CapabilityRun)
        .where(CapabilityRun.config_id == config.id)
        .order_by(CapabilityRun.id.desc())
        .limit(1)
    )
    readiness = _capability_runtime_readiness(template, config, db) if template else None
    return {
        "id": config.id,
        "name": config.name,
        "scope_type": config.scope_type,
        "research_case_id": config.research_case_id,
        "status": config.status,
        "config": config.config,
        "template": {
            "id": template.id,
            "catalog_key": template.catalog_key,
            "name": template.name,
            "display_name": _material_capability_name(template),
            "version": template.version,
            "description": template.description,
            "category": template.category,
            "validation_status": template.validation_status,
            "visibility": template.visibility,
            "input_schema": template.input_schema,
            "output_schema": template.output_schema,
        }
        if template
        else None,
        "latest_revision": latest_revision.revision_no if latest_revision else 0,
        "run_count": run_count,
        "can_run": readiness["can_run"] if readiness else False,
        "verified_usable": readiness["verified_usable"] if readiness else False,
        "availability_status": readiness["availability_status"] if readiness else "not_ready",
        "missing_prerequisites": readiness["missing_prerequisites"] if readiness else [],
        "verified_run": (
            _run_summary(readiness["verified_run"])
            if readiness and readiness["verified_run"] is not None
            else None
        ),
        "latest_run": _run_summary(latest_run) if latest_run else None,
        "updated_at": config.updated_at,
    }


@router.get("/capability-configs/{config_id}")
def get_capability_config(config_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    config = db.get(CapabilityConfig, config_id)
    if config is None or not _capability_config_visible(db, config, actor):
        raise HTTPException(status_code=404, detail="configuration not found")
    return _capability_config_detail(db, config)


@router.get("/capability-configs/{config_id}/revisions")
def list_capability_config_revisions(config_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    config = db.get(CapabilityConfig, config_id)
    if config is None or not _capability_config_visible(db, config, actor):
        raise HTTPException(status_code=404, detail="configuration not found")
    rows = list(
        db.scalars(
            select(CapabilityConfigRevision)
            .where(CapabilityConfigRevision.config_id == config_id)
            .order_by(CapabilityConfigRevision.revision_no.desc())
        )
    )
    return {
        "items": [
            {
                "id": item.id,
                "revision_no": item.revision_no,
                "name": item.name,
                "config_snapshot": item.config_snapshot,
                "template_snapshot": item.template_snapshot,
                "created_by": item.created_by,
                "created_at": item.created_at,
            }
            for item in rows
        ],
        "count": len(rows),
    }


@router.get("/capability-configs/{config_id}/runs")
def list_capability_config_runs(config_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    config = db.get(CapabilityConfig, config_id)
    if config is None or not _capability_config_visible(db, config, actor):
        raise HTTPException(status_code=404, detail="configuration not found")
    rows = list(
        db.scalars(
            select(CapabilityRun)
            .where(CapabilityRun.config_id == config_id)
            .order_by(CapabilityRun.created_at.desc(), CapabilityRun.id.desc())
        )
    )
    return {"items": [_run_summary(item) for item in rows], "count": len(rows)}


@router.get("/capability-reviews")
def list_capability_reviews(
    db: DbSession,
    actor: ReaderActor,
    target_type: Annotated[str | None, Query(max_length=32)] = None,
    target_id: Annotated[int | None, Query(gt=0)] = None,
) -> dict[str, Any]:
    rows = list(db.scalars(select(CapabilityReview).order_by(CapabilityReview.created_at.desc())))
    visible = []
    for item in rows:
        if target_type and item.target_type != target_type:
            continue
        if target_id and item.target_id != target_id:
            continue
        allowed = item.reviewed_by == actor.id
        if item.target_type == "capability_config":
            config = db.get(CapabilityConfig, item.target_id)
            allowed = config is not None and _capability_config_visible(db, config, actor)
        elif item.target_type == "capability_run":
            run = db.get(CapabilityRun, item.target_id)
            config = db.get(CapabilityConfig, run.config_id) if run else None
            allowed = config is not None and _capability_config_visible(db, config, actor)
        elif item.target_type == "capability_template":
            template = db.get(CapabilityTemplate, item.target_id)
            allowed = actor.role == "admin" or (template is not None and template.owner_id == actor.id)
        if allowed:
            visible.append(item)
    return {
        "items": [
            {
                "id": item.id,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "review_type": item.review_type,
                "status": item.status,
                "checklist": item.checklist,
                "note": item.note,
                "reviewed_by": item.reviewed_by,
                "reviewed_at": item.reviewed_at,
            }
            for item in visible
        ],
        "count": len(visible),
    }


@router.post(
    "/capability-configs/{config_id}/preview",
    dependencies=[Depends(require_reader_write_access)],
)
def preview_capability_run(
    config_id: int,
    payload: CapabilityPreviewPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    config = db.get(CapabilityConfig, config_id)
    template = db.get(CapabilityTemplate, config.template_id) if config else None
    if config is None or template is None or not _capability_config_visible(db, config, actor):
        raise HTTPException(status_code=404, detail="configuration not found")
    case_id = payload.research_case_id or config.research_case_id
    if case_id is not None:
        _case_permission(db, case_id, actor, "run_skill")
    effective = _validated_capability_config(template, {**config.config, **payload.input_overrides})
    if case_id is not None and "research_case_id" in set(template.input_schema.get("allowed_fields") or []):
        effective["research_case_id"] = case_id
    required = list(template.input_schema.get("required") or [])
    missing = [item for item in required if effective.get(item) in (None, "", [])]
    for group in template.input_schema.get("required_any_of") or []:
        if not any(effective.get(item) not in (None, "", []) for item in group):
            missing.append(" / ".join(group))
    checks = {
        "template_verified": template.validation_status == "verified",
        "execution_allowlisted": (template.execution_plan or {}).get("mode")
        in {"server_allowlist", "internal_compatibility"},
        "configuration_active": config.status in {"active", "published"},
        "project_scope_authorized": case_id is None or config.research_case_id in {None, case_id},
        "rights_policy": "scoped_evidence_only",
        "model_policy": "controlled_runtime_no_arbitrary_network",
    }
    can_run = all(value is True for value in checks.values() if isinstance(value, bool)) and not missing
    return {
        "config_id": config.id,
        "catalog_key": template.catalog_key,
        "research_case_id": case_id,
        "config_revision": _capability_config_detail(db, config)["latest_revision"],
        "effective_input": effective,
        "checks": checks,
        "missing_inputs": missing,
        "can_run": can_run,
        "writeback_requires_human_confirmation": True,
    }


@router.get("/capabilities")
def list_capabilities(db: DbSession, actor: OptionalReaderActor) -> list[dict[str, Any]]:
    active_configs = [
        item
        for item in db.scalars(
            select(CapabilityConfig)
            .where(CapabilityConfig.status.in_(("draft", "active", "pending_review", "published", "paused")))
            .order_by(CapabilityConfig.template_id, CapabilityConfig.id)
        )
        if _capability_config_visible(db, item, actor)
    ]
    configs_by_template: dict[int, list[CapabilityConfig]] = defaultdict(list)
    for config in active_configs:
        configs_by_template[config.template_id].append(config)
    rows = db.execute(
        select(CapabilityTemplate, func.count(CapabilityConfig.id))
        .outerjoin(CapabilityConfig, CapabilityConfig.template_id == CapabilityTemplate.id)
        .where(
            CapabilityTemplate.status.in_(("active", "draft")),
            CapabilityTemplate.visibility == "public",
            CapabilityTemplate.catalog_key.is_not(None),
        )
        .group_by(CapabilityTemplate.id)
        .order_by(CapabilityTemplate.slug, CapabilityTemplate.version.desc())
    )
    response: list[dict[str, Any]] = []
    for template, config_count in rows:
        configs = []
        for config in configs_by_template[template.id]:
            latest_run = db.scalar(
                select(CapabilityRun)
                .where(CapabilityRun.config_id == config.id)
                .order_by(CapabilityRun.id.desc())
                .limit(1)
            )
            prerequisites, can_run = _capability_prerequisites(template, config, db)
            can_run = (
                can_run
                and template.validation_status == "verified"
                and config.status in {"active", "published"}
            )
            configs.append(
                {
                    "id": config.id,
                    "name": config.name,
                    "config": config.config,
                    "scope_type": config.scope_type,
                    "research_case_id": config.research_case_id,
                    "status": config.status,
                    "prerequisites": prerequisites,
                    "can_run": can_run,
                    "latest_run": _run_summary(latest_run) if latest_run else None,
                }
            )
        response.append(
            {
                "id": template.id,
                "catalog_key": template.catalog_key,
                "slug": template.catalog_key,
                "version": template.version,
                "name": template.name,
                "display_name": _material_capability_name(template),
                "description": template.description,
                "status": template.status,
                "input_schema": template.input_schema,
                "output_schema": template.output_schema,
                "default_config": template.default_config,
                "skill_manifest": {
                    **_skill_manifest_v2(template),
                    "skill_code": None,
                    "catalog_key": template.catalog_key,
                },
                "category": template.category,
                "validation_status": template.validation_status,
                "can_run": template.validation_status == "verified",
                "catalog_visibility": "public_catalog",
                "config_count": config_count,
                "configs": configs,
                "execution_mode": "internal_declarative_only",
            }
        )
    return response


def _validated_capability_config(
    template: CapabilityTemplate,
    config: dict[str, Any],
) -> dict[str, Any]:
    if _contains_forbidden_skill_key(config):
        raise HTTPException(status_code=422, detail="capability config must remain declarative")
    allowed_fields = set(template.input_schema.get("allowed_fields") or [])
    unknown = set(config) - allowed_fields
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported capability config fields: {', '.join(sorted(unknown))}",
        )
    return config


@router.post(
    "/skills/drafts",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def submit_skill_draft(payload: SkillDraftPayload, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    if payload.manifest.skill_code is not None:
        raise HTTPException(status_code=422, detail="新能力包必须使用 catalog_key，S 编号仅供历史执行器兼容")
    existing = db.scalar(
        select(CapabilityTemplate).where(
            CapabilityTemplate.slug == payload.slug,
            CapabilityTemplate.version == payload.version,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Skill slug and version already exist")
    manifest = payload.manifest.model_dump(mode="json")
    template = CapabilityTemplate(
        catalog_key=payload.manifest.catalog_key,
        owner_id=actor.id,
        slug=payload.slug,
        version=payload.version,
        name=payload.name,
        description=payload.description,
        input_schema=payload.input_schema,
        output_schema=payload.output_schema,
        default_config={"skill": manifest, "submission_mode": "declarative_json_only"},
        status="draft",
        visibility="private",
        validation_status="pending",
        execution_plan={"mode": "server_allowlist", "executors": []},
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return {
        "id": template.id,
        "slug": template.slug,
        "version": template.version,
        "status": template.status,
        "skill_manifest": _skill_manifest_v2(template),
        "review_required": True,
        "execution_mode": "internal_declarative_only",
    }


@router.post("/skills/preflight", dependencies=[Depends(require_reader_write_access)])
def preflight_skill_draft(payload: SkillDraftPayload, actor: ReaderActor) -> dict[str, Any]:
    if payload.manifest.skill_code is not None:
        raise HTTPException(status_code=422, detail="new capability packages must use catalog_key")
    return {
        "accepted": True,
        "catalog_key": payload.manifest.catalog_key,
        "checks": {
            "declarative_json_only": True,
            "no_code_or_shell": True,
            "no_database_commands": True,
            "no_arbitrary_network": True,
            "no_credentials_or_private_binding": True,
            "human_writeback_checkpoint": bool(payload.manifest.human_checkpoints),
        },
        "next_action": "submit_draft_for_review",
    }


@router.get("/skills/{template_id}/manifest")
def export_skill_manifest(template_id: int, db: DbSession) -> dict[str, Any]:
    template = db.get(CapabilityTemplate, template_id)
    if template is None or template.status == "retired":
        raise HTTPException(status_code=404, detail="Skill not found")
    return {
        "slug": template.slug,
        "version": template.version,
        "name": template.name,
        "display_name": _material_capability_name(template),
        "description": template.description,
        "status": template.status,
        "visibility": template.visibility,
        "validation_status": template.validation_status,
        "input_schema": template.input_schema,
        "output_schema": template.output_schema,
        "manifest": _skill_manifest_v2(template),
        "format": "SkillManifestV2.declarative-json",
    }


@router.post(
    "/capabilities/{template_id}/configs",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def install_capability_config(
    template_id: int,
    payload: CapabilityConfigPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    template = db.get(CapabilityTemplate, template_id)
    if template is None or template.status != "active":
        raise HTTPException(status_code=404, detail="active Skill not found")
    if db.scalar(
        select(CapabilityConfig.id).where(
            CapabilityConfig.owner_id == actor.id,
            CapabilityConfig.scope_type == "personal",
            CapabilityConfig.name == payload.name,
        )
    ):
        raise HTTPException(status_code=409, detail="configuration name already exists")
    config = CapabilityConfig(
        template_id=template.id,
        owner_id=actor.id,
        scope_type="personal",
        name=payload.name,
        config=_validated_capability_config(template, payload.config),
        status="active" if template.validation_status == "verified" else "draft",
    )
    db.add(config)
    db.flush()
    revision = _create_capability_config_revision(db, config, template, actor.id)
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="created",
            target_type="capability_config",
            target_id=config.id,
            details={"revision_id": revision.id},
        )
    )
    db.commit()
    db.refresh(config)
    return {"id": config.id, "name": config.name, "config": config.config, "status": config.status}


@router.patch(
    "/capability-configs/{config_id}",
    dependencies=[Depends(require_reader_write_access)],
)
def update_capability_config(
    config_id: int,
    payload: CapabilityConfigPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    config = db.get(CapabilityConfig, config_id)
    if config is None or config.status in {"archived", "disabled"}:
        raise HTTPException(status_code=404, detail="configuration not found")
    if not _capability_config_visible(db, config, actor, "edit"):
        raise HTTPException(status_code=403, detail="configuration is outside the current user's scope")
    template = db.get(CapabilityTemplate, config.template_id)
    config.name = payload.name
    config.config = _validated_capability_config(template, payload.config)
    revision = _create_capability_config_revision(db, config, template, actor.id)
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="configured",
            target_type="capability_config",
            target_id=config.id,
            details={"revision_id": revision.id, "revision_no": revision.revision_no},
        )
    )
    db.commit()
    return {
        "id": config.id,
        "name": config.name,
        "config": config.config,
        "status": config.status,
        "revision": revision.revision_no,
    }


@router.post(
    "/capability-configs/{config_id}/status",
    dependencies=[Depends(require_reader_write_access)],
)
def transition_capability_config(
    config_id: int,
    payload: CapabilityConfigStatusPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    config = db.get(CapabilityConfig, config_id)
    if config is None or not _capability_config_visible(db, config, actor, "edit"):
        raise HTTPException(status_code=404, detail="configuration not found")
    if payload.status == "archived" and config.research_case_id is not None:
        _case_permission(db, config.research_case_id, actor, "archive")
    transitions = {
        "draft": {"pending_review", "archived"},
        "active": {"paused", "archived"},
        "pending_review": {"archived"},
        "published": {"paused", "archived"},
        "paused": {"active", "archived"},
    }
    if payload.status == config.status:
        return {"id": config.id, "status": config.status, "idempotent_replay": True}
    if payload.status not in transitions.get(config.status, set()):
        raise HTTPException(
            status_code=409,
            detail=f"configuration cannot transition from {config.status} to {payload.status}",
        )
    template = db.get(CapabilityTemplate, config.template_id)
    if payload.status == "active" and template.validation_status != "verified":
        raise HTTPException(status_code=409, detail="unverified capability cannot be activated")
    before = config.status
    config.status = payload.status
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="archived" if payload.status == "archived" else payload.status,
            target_type="capability_config",
            target_id=config.id,
            details={"before": before, "after": config.status},
        )
    )
    db.commit()
    return {"id": config.id, "status": config.status, "previous_status": before}


@router.post(
    "/capability-reviews",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def review_capability(
    payload: CapabilityReviewPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    target: CapabilityTemplate | CapabilityConfig | CapabilityRun | None
    target = None
    if payload.target_type == "capability_template":
        target = db.get(CapabilityTemplate, payload.target_id)
        if target is None or actor.role != "admin":
            raise HTTPException(status_code=403, detail="public capability review requires an admin")
        if payload.review_type not in {"validation", "public_publish"}:
            raise HTTPException(status_code=422, detail="invalid template review type")
        target.validation_status = "verified" if payload.status == "approved" else "failed"
        if payload.review_type == "public_publish" and payload.status == "approved":
            target.visibility = "public"
            target.status = "active"
    elif payload.target_type == "capability_config":
        target = db.get(CapabilityConfig, payload.target_id)
        if target is None or target.research_case_id is None:
            raise HTTPException(status_code=404, detail="project capability configuration not found")
        _case_permission(db, target.research_case_id, actor, "confirm")
        if payload.review_type != "team_publish":
            raise HTTPException(status_code=422, detail="invalid configuration review type")
        template = db.get(CapabilityTemplate, target.template_id)
        if payload.status == "approved" and template.validation_status != "verified":
            raise HTTPException(status_code=409, detail="unverified capability cannot be published")
        target.status = "published" if payload.status == "approved" else "draft"
    else:
        target = db.get(CapabilityRun, payload.target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="capability run not found")
        if target.research_case_id is not None:
            _case_permission(db, target.research_case_id, actor, "confirm")
        else:
            config = db.get(CapabilityConfig, target.config_id)
            if config is None or config.owner_id != actor.id:
                raise HTTPException(
                    status_code=403, detail="capability run is outside the current user's scope"
                )
        if payload.review_type != "run":
            raise HTTPException(status_code=422, detail="invalid run review type")
        target.review_status = payload.status
        target.reviewed_by = actor.id
        target.reviewed_at = datetime.now(UTC)
    review = CapabilityReview(
        target_type=payload.target_type,
        target_id=payload.target_id,
        review_type=payload.review_type,
        status=payload.status,
        checklist=payload.checklist,
        note=payload.note,
        reviewed_by=actor.id,
        reviewed_at=datetime.now(UTC),
    )
    db.add(review)
    db.flush()
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="reviewed",
            target_type=payload.target_type,
            target_id=payload.target_id,
            details={
                "review_id": review.id,
                "review_type": payload.review_type,
                "status": payload.status,
            },
        )
    )
    db.commit()
    return {
        "id": review.id,
        "target_type": review.target_type,
        "target_id": review.target_id,
        "review_type": review.review_type,
        "status": review.status,
    }


def _advance_schedule(item: CapabilitySchedule) -> datetime:
    current = item.next_run_at or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (
        current
        + {
            "daily": timedelta(days=1),
            "weekly": timedelta(days=7),
            "monthly": timedelta(days=30),
        }[item.cadence]
    )


@router.post(
    "/capability-configs/{config_id}/schedules",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def create_capability_schedule(
    config_id: int,
    payload: CapabilitySchedulePayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    existing = db.scalar(
        select(CapabilitySchedule).where(CapabilitySchedule.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        if existing.config_id != config_id:
            raise HTTPException(status_code=409, detail="idempotency key belongs to another schedule")
        return {"id": existing.id, "status": existing.status, "idempotent_replay": True}
    config = db.get(CapabilityConfig, config_id)
    template = db.get(CapabilityTemplate, config.template_id) if config else None
    if config is None or template is None or not _capability_config_visible(db, config, actor, "edit"):
        raise HTTPException(status_code=404, detail="configuration not found")
    if config.status not in {"active", "published"} or template.validation_status != "verified":
        raise HTTPException(status_code=409, detail="only verified active capabilities can be scheduled")
    item = CapabilitySchedule(
        config_id=config.id,
        owner_id=actor.id,
        cadence=payload.cadence,
        timezone=payload.timezone,
        next_run_at=payload.next_run_at,
        status="active",
        idempotency_key=payload.idempotency_key,
    )
    db.add(item)
    db.flush()
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="scheduled",
            target_type="capability_schedule",
            target_id=item.id,
            details={"config_id": config.id, "cadence": item.cadence},
        )
    )
    db.commit()
    return {"id": item.id, "status": item.status, "next_run_at": item.next_run_at}


@router.get("/capability-schedules")
def list_capability_schedules(db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    rows = []
    for item in db.scalars(select(CapabilitySchedule).order_by(CapabilitySchedule.created_at.desc())):
        config = db.get(CapabilityConfig, item.config_id)
        if config is not None and _capability_config_visible(db, config, actor):
            rows.append(item)
    return {
        "items": [
            {
                "id": item.id,
                "config_id": item.config_id,
                "cadence": item.cadence,
                "timezone": item.timezone,
                "next_run_at": item.next_run_at,
                "status": item.status,
            }
            for item in rows
        ],
        "count": len(rows),
    }


@router.patch(
    "/capability-schedules/{schedule_id}",
    dependencies=[Depends(require_reader_write_access)],
)
def update_capability_schedule(
    schedule_id: int,
    payload: CapabilityScheduleStatusPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    item = db.get(CapabilitySchedule, schedule_id)
    config = db.get(CapabilityConfig, item.config_id) if item else None
    if item is None or config is None or not _capability_config_visible(db, config, actor, "edit"):
        raise HTTPException(status_code=404, detail="schedule not found")
    item.status = payload.status
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="schedule_status",
            target_type="capability_schedule",
            target_id=item.id,
            details={"status": item.status},
        )
    )
    db.commit()
    return {"id": item.id, "status": item.status}


@router.post(
    "/capability-schedules/{schedule_id}/claim",
    dependencies=[Depends(require_reader_write_access)],
)
def claim_capability_schedule(
    schedule_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    item = db.scalar(select(CapabilitySchedule).where(CapabilitySchedule.id == schedule_id).with_for_update())
    if item is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    config = db.get(CapabilityConfig, item.config_id)
    if config is None or not _capability_config_visible(db, config, actor, "edit"):
        raise HTTPException(status_code=404, detail="schedule not found")
    now = datetime.now(UTC)
    next_run_at = item.next_run_at
    comparable_next = (
        next_run_at.replace(tzinfo=UTC)
        if next_run_at is not None and next_run_at.tzinfo is None
        else next_run_at
    )
    if item.status != "active" or comparable_next is None or comparable_next > now:
        return {"id": item.id, "claimed": False, "next_run_at": next_run_at}
    item.next_run_at = _advance_schedule(item)
    db.flush()
    try:
        run = run_capability(
            db,
            config_id=config.id,
            research_case_id=config.research_case_id,
            requested_by=actor.id,
        )
    except ResearchCapabilityError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run.schedule_id = item.id
    run.review_status = "pending"
    revision = db.scalar(
        select(CapabilityConfigRevision)
        .where(CapabilityConfigRevision.config_id == config.id)
        .order_by(CapabilityConfigRevision.revision_no.desc())
        .limit(1)
    )
    if revision is not None:
        run.config_revision_id = revision.id
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="schedule_claimed",
            target_type="capability_schedule",
            target_id=item.id,
            details={"run_id": run.id, "scheduled_for": next_run_at.isoformat()},
        )
    )
    db.commit()
    return {
        "id": item.id,
        "claimed": True,
        "run": _run_summary(run),
        "next_run_at": item.next_run_at,
        "writeback_pending_review": True,
    }


@router.get("/capability-audit-events")
def list_capability_audit_events(
    db: DbSession,
    actor: ReaderActor,
    config_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    visible_config_ids = [
        item.id
        for item in db.scalars(select(CapabilityConfig))
        if _capability_config_visible(db, item, actor)
    ]
    if config_id is not None and config_id not in visible_config_ids:
        raise HTTPException(status_code=404, detail="configuration not found")
    statement = select(CapabilityAuditEvent).where(
        or_(
            and_(
                CapabilityAuditEvent.target_type == "capability_config",
                CapabilityAuditEvent.target_id.in_(visible_config_ids or [-1]),
            ),
            CapabilityAuditEvent.actor_id == actor.id,
        )
    )
    if config_id is not None:
        statement = statement.where(
            CapabilityAuditEvent.target_type == "capability_config",
            CapabilityAuditEvent.target_id == config_id,
        )
    rows = list(db.scalars(statement.order_by(CapabilityAuditEvent.created_at.desc()).limit(limit)))
    return {
        "items": [
            {
                "id": item.id,
                "action": item.action,
                "target_type": item.target_type,
                "target_id": item.target_id,
                "details": item.details,
                "created_at": item.created_at,
            }
            for item in rows
        ],
        "count": len(rows),
    }


@router.post(
    "/agent-runs/{run_id}/skill-draft",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def create_skill_draft_from_agent_run(run_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)
    if run is None or run.status != "succeeded" or not run.artifact:
        raise HTTPException(status_code=422, detail="only a successful agent run can become a skill draft")
    if run.requested_by not in {None, actor.id}:
        raise HTTPException(status_code=403, detail="assistant run is owned by another user")
    for template in db.scalars(select(CapabilityTemplate).where(CapabilityTemplate.status == "draft")):
        if template.default_config.get("source_agent_run_id") == run.id:
            return {
                "id": template.id,
                "slug": template.slug,
                "name": template.name,
                "display_name": _material_capability_name(template),
                "status": template.status,
                "idempotent_replay": True,
            }
    plan = run.plan or {}
    selected = list(plan.get("selected_capabilities") or [])
    public_steps = list(plan.get("public_steps") or [])
    question = run.question.strip()
    name = (question[:54] + ("…" if len(question) > 54 else "")) or f"研究工作流 #{run.id}"
    manifest = {
        "purpose": f"复用运行 #{run.id} 已验证的研究路径。",
        "when_to_use": [question],
        "supported_scopes": [run.scope_type],
        "steps": public_steps or [f"调用 {item}" for item in selected],
        "tool_permissions": selected,
        "evidence_policy": "研究对话允许基于已引用事实综合表达；正式产物仍执行严格事实—证据校验。",
        "starter_prompt": question,
        "source_run_id": run.id,
    }
    template = CapabilityTemplate(
        slug=f"saved-agent-run-{run.id}",
        version="0.1.0",
        name=name,
        description=f"由成功的 {run.scope_type} 研究对话总结生成，等待人工审核后复用。",
        input_schema={
            "allowed_fields": ["scope_context", "question"],
            "required": ["scope_context"],
        },
        output_schema={"type": "agent_research_answer", "source_trace_required": True},
        default_config={
            "source_agent_run_id": run.id,
            "scope_context": run.scope_context,
            "skill": manifest,
        },
        status="draft",
        owner_id=run.requested_by,
        visibility="private",
        validation_status="pending",
        execution_plan={"mode": "server_allowlist", "executors": selected},
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return {
        "id": template.id,
        "slug": template.slug,
        "name": template.name,
        "display_name": _material_capability_name(template),
        "status": template.status,
        "skill_manifest": manifest,
        "idempotent_replay": False,
    }


@router.post(
    "/skills/{template_id}/activate",
    dependencies=[Depends(require_reader_write_access)],
)
def activate_skill_draft(template_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    template = db.get(CapabilityTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if template.status == "retired":
        raise HTTPException(status_code=409, detail="retired skill cannot be activated")
    if template.owner_id not in {None, actor.id} and actor.role != "admin":
        raise HTTPException(status_code=403, detail="skill draft is owned by another user")
    approved = db.scalar(
        select(CapabilityReview.id).where(
            CapabilityReview.target_type == "capability_template",
            CapabilityReview.target_id == template.id,
            CapabilityReview.review_type.in_(("team_publish", "public_publish", "validation")),
            CapabilityReview.status == "approved",
        )
    )
    if approved is None:
        raise HTTPException(
            status_code=409, detail="skill draft requires an approved review before activation"
        )
    template.status = "active"
    db.commit()
    return {
        "id": template.id,
        "slug": template.slug,
        "name": template.name,
        "display_name": _material_capability_name(template),
        "status": template.status,
        "skill_manifest": _skill_manifest_v2(template),
    }


class CapabilityRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: int = Field(gt=0)
    research_case_id: int | None = Field(default=None, gt=0)
    input_overrides: dict[str, Any] = Field(default_factory=dict)


class CapabilityRunRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: dict[str, Any]
    note: str = Field(default="", max_length=2000)
    base_revision_no: int | None = Field(default=None, ge=0)


@router.post("/capability-runs", dependencies=[Depends(require_reader_write_access)])
def create_capability_run(
    payload: CapabilityRunPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    config = db.get(CapabilityConfig, payload.config_id)
    if config is None or config.status not in {"active", "published"}:
        raise HTTPException(status_code=409, detail="capability configuration is not runnable")
    template = db.get(CapabilityTemplate, config.template_id)
    if template is None or template.status != "active":
        raise HTTPException(status_code=409, detail="capability template is not published")
    permission = "run_skill" if config.scope_type == "research_case" else "view"
    config_visible = _capability_config_visible(db, config, actor, permission)
    if not config_visible and payload.research_case_id is not None and template.visibility == "internal":
        research_case = db.get(ResearchCase, payload.research_case_id)
        config_visible = research_case is not None and config.owner_id == research_case.owner_id
        if config_visible:
            _case_permission(db, research_case.id, actor, "run_skill")
    if not config_visible:
        raise HTTPException(
            status_code=403, detail="capability configuration is outside the current user's scope"
        )
    if config.scope_type == "research_case" and payload.research_case_id != config.research_case_id:
        raise HTTPException(
            status_code=422, detail="project capability must run inside its configured project"
        )
    if template.visibility == "public" and template.validation_status != "verified":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "capability_not_verified",
                "validation_status": template.validation_status,
                "missing_acceptance": _catalog_capability_payload(template)["missing_acceptance"],
            },
        )
    if (template.execution_plan or {}).get("mode") not in {
        "server_allowlist",
        "internal_compatibility",
    }:
        raise HTTPException(status_code=422, detail="capability execution plan is not allowlisted")
    if payload.research_case_id is not None:
        _case_permission(db, payload.research_case_id, actor, "run_skill")
    effective_workflow = {**config.config, **payload.input_overrides}
    if effective_workflow.get("workflow_version") == 1:
        from app.api.skill_workflows import WorkflowConfig, material_access

        try:
            effective_workflow = WorkflowConfig.model_validate(effective_workflow).model_dump()
        except ValueError as exc:
            raise HTTPException(422, "工作流输入字段无效") from exc
        for material_id in effective_workflow.get("material_ids", []):
            material = material_access(db, material_id, actor)
            if material.research_case_id != config.research_case_id:
                raise HTTPException(422, "只可处理当前项目选定材料")
    try:
        run = run_capability(
            db,
            config_id=payload.config_id,
            research_case_id=payload.research_case_id,
            input_overrides=payload.input_overrides,
            requested_by=actor.id,
        )
    except ResearchCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    revision = db.scalar(
        select(CapabilityConfigRevision)
        .where(CapabilityConfigRevision.config_id == config.id)
        .order_by(CapabilityConfigRevision.revision_no.desc())
        .limit(1)
    )
    if revision is None:
        revision = _create_capability_config_revision(db, config, template, actor.id)
    if run.config_revision_id is None:
        run.config_revision_id = revision.id
    run.review_status = "pending"
    run.execution_summary = {
        "executor_mode": (template.execution_plan or {}).get("mode"),
        "catalog_key": template.catalog_key,
        "config_revision": revision.revision_no,
        "cost": "recorded_after_execution",
    }
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="run",
            target_type="capability_run",
            target_id=run.id,
            details={"config_id": config.id, "config_revision_id": revision.id},
        )
    )
    if run.research_case_id:
        record_contribution(
            db,
            research_case_id=run.research_case_id,
            user_id=actor.id,
            action_type="capability_run_created",
            object_type="capability_run",
            object_key=run.id,
            details={"status": run.status},
        )
    db.commit()
    from app.services.field_access import enforce_run_access

    enforce_run_access(db, run, actor)
    return {
        **_run_summary(run),
        "input_snapshot": run.input_snapshot,
        "output": run.output,
        "artifact_ref": run.artifact_ref,
    }


@router.get("/capability-runs/{run_id}")
def get_capability_run(run_id: int, db: DbSession, actor: ReaderActor) -> dict[str, Any]:
    run = db.get(CapabilityRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="capability run not found")
    config = _require_capability_run_access(db, run, actor)
    template = db.get(CapabilityTemplate, config.template_id) if config else None
    declared_targets = set(_skill_manifest_v2(template).get("writeback_targets") or []) if template else set()
    configured_country = str(
        ((run.input_snapshot or {}).get("config") or {}).get("country_iso3") or ""
    ).upper()
    writeback_options: list[dict[str, str]] = []
    if run.research_case_id and "research_case" in declared_targets:
        writeback_options.append(
            {
                "target_type": "research_case",
                "target_key": str(run.research_case_id),
                "label": "写回当前项目",
            }
        )
    if configured_country and "country" in declared_targets:
        writeback_options.append(
            {
                "target_type": "country",
                "target_key": configured_country,
                "label": f"写回国别 {configured_country}",
            }
        )
    targets = list(
        db.scalars(
            select(CapabilityRunTarget)
            .where(CapabilityRunTarget.capability_run_id == run.id)
            .order_by(CapabilityRunTarget.id)
        )
    )
    return {
        **_run_summary(run),
        "input_snapshot": run.input_snapshot,
        "output": run.output,
        "artifact_ref": run.artifact_ref,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "requested_by": run.requested_by,
        "writeback_options": writeback_options,
        "writeback_targets": [
            {
                "id": item.id,
                "target_type": item.target_type,
                "target_key": item.target_key,
                "confirmed_at": item.confirmed_at,
            }
            for item in targets
        ],
    }


@router.get("/capability-runs/{run_id}/revisions")
def list_capability_run_revisions(
    run_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> list[dict[str, Any]]:
    run = db.get(CapabilityRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="capability run not found")
    _require_capability_run_access(db, run, actor)
    rows = db.scalars(
        select(CapabilityRunRevision)
        .where(CapabilityRunRevision.capability_run_id == run_id)
        .order_by(CapabilityRunRevision.revision_no)
    )
    return [
        {
            "id": item.id,
            "revision_no": item.revision_no,
            "artifact": item.artifact,
            "note": item.note,
            "created_by": item.created_by,
            "created_at": item.created_at,
        }
        for item in rows
    ]


@router.post(
    "/capability-runs/{run_id}/revisions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_reader_write_access)],
)
def create_capability_run_revision(
    run_id: int,
    payload: CapabilityRunRevisionPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    run = db.get(CapabilityRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="capability run not found")
    _require_capability_run_access(db, run, actor, "revise")
    if run.status != "succeeded":
        raise HTTPException(status_code=422, detail="只能修订成功运行的候选结果")
    original_type = (run.output or {}).get("type")
    if original_type in {"field_research_workflow", "policy_tracking_workflow"}:
        raise HTTPException(422, "请使用专用工作流编辑与确认接口")
    if payload.artifact.get("type") != original_type:
        raise HTTPException(status_code=422, detail="修订不得改变产物类型")
    latest_no = (
        db.scalar(
            select(func.max(CapabilityRunRevision.revision_no)).where(
                CapabilityRunRevision.capability_run_id == run_id
            )
        )
        or 0
    )
    if payload.base_revision_no is not None and payload.base_revision_no != latest_no:
        raise HTTPException(status_code=409, detail="候选结果已有新修订，请刷新后再编辑")
    revision = CapabilityRunRevision(
        capability_run_id=run.id,
        revision_no=latest_no + 1,
        artifact=payload.artifact,
        note=payload.note.strip(),
        created_by=actor.id,
    )
    db.add(revision)
    db.flush()
    if run.research_case_id:
        record_contribution(
            db,
            research_case_id=run.research_case_id,
            user_id=actor.id,
            action_type="capability_run_revised",
            object_type="capability_run_revision",
            object_key=revision.id,
            details={"run_id": run.id, "revision_no": revision.revision_no},
        )
    db.commit()
    return {
        "id": revision.id,
        "revision_no": revision.revision_no,
        "artifact": revision.artifact,
        "note": revision.note,
        "created_by": revision.created_by,
        "created_at": revision.created_at,
    }


@router.get("/research-cases/{case_id}/policy-impact-graph")
def get_policy_impact_graph(
    case_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "view")
    run = db.scalar(
        select(CapabilityRun)
        .join(CapabilityConfig, CapabilityConfig.id == CapabilityRun.config_id)
        .join(CapabilityTemplate, CapabilityTemplate.id == CapabilityConfig.template_id)
        .where(
            CapabilityRun.research_case_id == case_id,
            CapabilityTemplate.slug == "policy-impact-graph",
        )
        .order_by(CapabilityRun.id.desc())
        .limit(1)
    )
    if run is None:
        return {"research_case_id": case_id, "latest_run": None, "graph": None}
    revision = db.scalar(
        select(CapabilityRunRevision)
        .where(CapabilityRunRevision.capability_run_id == run.id)
        .order_by(CapabilityRunRevision.revision_no.desc())
        .limit(1)
    )
    return {
        "research_case_id": case_id,
        "latest_run": _run_summary(run),
        "graph": revision.artifact if revision else run.output,
        "revision": (
            {"id": revision.id, "revision_no": revision.revision_no, "note": revision.note}
            if revision
            else None
        ),
    }


@router.post(
    "/research-cases/{case_id}/policy-impact-graph/runs",
    dependencies=[Depends(require_reader_write_access)],
)
def create_policy_impact_graph_run(
    case_id: int,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    _case_permission(db, case_id, actor, "run_skill")
    config = db.scalar(
        select(CapabilityConfig)
        .join(CapabilityTemplate, CapabilityTemplate.id == CapabilityConfig.template_id)
        .where(
            CapabilityTemplate.slug == "policy-impact-graph",
            CapabilityTemplate.status == "active",
            CapabilityConfig.status == "active",
        )
        .order_by(CapabilityConfig.id)
        .limit(1)
    )
    if config is None:
        raise HTTPException(status_code=409, detail="政策关系图功能尚未初始化")
    try:
        run = run_capability(
            db,
            config_id=config.id,
            research_case_id=case_id,
            requested_by=actor.id,
        )
    except ResearchCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="policy_graph_generated",
        object_type="capability_run",
        object_key=run.id,
        details={"status": run.status},
    )
    db.commit()
    return {**_run_summary(run), "graph": run.output}


@router.post(
    "/capability-runs/{run_id}/writeback",
    dependencies=[Depends(require_reader_write_access)],
)
def confirm_capability_run_writeback(
    run_id: int,
    payload: CapabilityRunWritebackPayload,
    db: DbSession,
    actor: ReaderActor,
) -> dict[str, Any]:
    existing = db.scalar(
        select(CapabilityRunTarget).where(CapabilityRunTarget.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        existing_run = db.get(CapabilityRun, existing.capability_run_id)
        if existing_run is None:
            raise HTTPException(status_code=404, detail="capability run not found")
        _require_capability_run_access(db, existing_run, actor, "confirm")
        if (
            existing.capability_run_id != run_id
            or existing.target_type != payload.target_type
            or existing.target_key != payload.target_key
            or (existing.artifact_snapshot or {}).get("revision_id") != payload.revision_id
        ):
            raise HTTPException(status_code=409, detail="idempotency key belongs to another writeback")
        return {
            "id": existing.id,
            "capability_run_id": existing.capability_run_id,
            "target_type": existing.target_type,
            "target_key": existing.target_key,
            "idempotent_replay": True,
        }
    run = db.get(CapabilityRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="capability run not found")
    config = _require_capability_run_access(db, run, actor, "confirm")
    if run.output.get("type") in {"field_research_workflow", "policy_tracking_workflow"}:
        raise HTTPException(422, "请从专用工作流保存已确认结果")
    if run.status != "succeeded":
        raise HTTPException(status_code=422, detail="only a successful run can be written back")
    template = db.get(CapabilityTemplate, config.template_id) if config else None
    allowed_targets = set(_skill_manifest_v2(template).get("writeback_targets") or []) if template else set()
    if payload.target_type not in allowed_targets:
        raise HTTPException(status_code=422, detail="target type is not declared by this Skill")
    if payload.target_type == "country":
        _require_catalog_country(payload.target_key.upper())
    else:
        try:
            numeric_key = int(payload.target_key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="target key must be a numeric id") from exc
        model = {
            "event": ResearchEvent,
            "research_case": ResearchCase,
            "document_version": DocumentVersion,
        }[payload.target_type]
        if db.get(model, numeric_key) is None:
            raise HTTPException(status_code=404, detail="writeback target not found")
    selected_output = run.output
    if payload.revision_id is not None:
        revision = db.get(CapabilityRunRevision, payload.revision_id)
        if revision is None or revision.capability_run_id != run.id:
            raise HTTPException(status_code=404, detail="运行修订版不存在")
        selected_output = revision.artifact
    target = CapabilityRunTarget(
        capability_run_id=run.id,
        target_type=payload.target_type,
        target_key=payload.target_key.upper() if payload.target_type == "country" else payload.target_key,
        idempotency_key=payload.idempotency_key,
        artifact_snapshot={
            "output": selected_output,
            "input_snapshot": run.input_snapshot,
            "confirmed_status": run.status,
            "revision_id": payload.revision_id,
        },
        confirmed_by=actor.id,
    )
    run.review_status = "approved"
    run.reviewed_by = actor.id
    run.reviewed_at = datetime.now(UTC)
    db.add(target)
    db.add(
        CapabilityReview(
            target_type="capability_run",
            target_id=run.id,
            review_type="run",
            status="approved",
            checklist={"human_writeback_confirmation": True},
            note="人工确认写回时同步完成运行审阅。",
            reviewed_by=actor.id,
            reviewed_at=run.reviewed_at,
        )
    )
    db.add(
        CapabilityAuditEvent(
            actor_id=actor.id,
            action="writeback",
            target_type="capability_run",
            target_id=run.id,
            details={"target_type": payload.target_type, "target_key": payload.target_key},
        )
    )
    if run.research_case_id:
        record_contribution(
            db,
            research_case_id=run.research_case_id,
            user_id=actor.id,
            action_type="capability_run_written_back",
            object_type="capability_run_target",
            object_key=payload.target_key,
            details={"run_id": run.id, "revision_id": payload.revision_id},
        )
    db.commit()
    db.refresh(target)
    return {
        "id": target.id,
        "capability_run_id": target.capability_run_id,
        "target_type": target.target_type,
        "target_key": target.target_key,
        "idempotent_replay": False,
    }


@router.get("/agent-runs/{run_id}")
def get_agent_run(
    run_id: int,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    _require_assistant_run_access(db, run, x_reader_key)
    return {
        "id": run.id,
        "country_iso3": run.country_iso3,
        "conversation_id": run.conversation_id,
        "scope_type": run.scope_type,
        "scope_key": run.scope_key,
        "scope_context": run.scope_context,
        "question": run.question,
        "workflow": run.workflow,
        "runtime": run.runtime,
        "status": run.status,
        "plan": run.plan,
        "tool_trace": run.tool_trace,
        "evidence": run.evidence,
        "artifact": run.artifact,
        "error_code": run.error_code,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "created_at": run.created_at,
    }


@router.post("/assistant/actions/{run_id}/{action_id}/confirm")
def confirm_assistant_action(
    run_id: int,
    action_id: str,
    db: DbSession,
    x_reader_key: Annotated[str | None, Header(alias="X-Reader-Key")] = None,
) -> dict[str, Any]:
    agent_run = db.scalar(select(AgentRun).where(AgentRun.id == run_id).with_for_update())
    if agent_run is None or agent_run.status != "succeeded":
        raise HTTPException(status_code=404, detail="successful assistant run not found")
    actor = _require_assistant_run_access(db, agent_run, x_reader_key, "run_skill")
    artifact = dict(agent_run.artifact or {})
    actions = [dict(item) for item in artifact.get("proposed_actions", [])]
    action = next((item for item in actions if item.get("action_id") == action_id), None)
    if action is None:
        raise HTTPException(status_code=404, detail="assistant action not found")
    if action.get("execution_result"):
        return {
            "assistant_run_id": agent_run.id,
            "action": action,
            "idempotent_replay": True,
        }
    if action.get("type") != "capability_run_execute":
        raise HTTPException(status_code=422, detail="unsupported assistant action")
    _require_assistant_case_access(
        db,
        action.get("research_case_id")
        or _assistant_case_id(
            agent_run.scope_type,
            agent_run.scope_key,
            agent_run.scope_context or {},
        ),
        x_reader_key,
        "run_skill",
    )
    try:
        capability_run = run_capability(
            db,
            config_id=int(action["config_id"]),
            research_case_id=action.get("research_case_id"),
            requested_by=actor.id,
        )
    except ResearchCapabilityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    action["status"] = "executed"
    action["execution_result"] = {
        "capability_run_id": capability_run.id,
        "status": capability_run.status,
        "finished_at": _iso_datetime(capability_run.finished_at),
    }
    artifact["proposed_actions"] = actions
    agent_run.artifact = artifact
    db.commit()
    return {
        "assistant_run_id": agent_run.id,
        "action": action,
        "idempotent_replay": False,
        "capability_run": {
            **_run_summary(capability_run),
            "input_snapshot": capability_run.input_snapshot,
            "output": capability_run.output,
        },
    }


def _event_summary(event: ResearchEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "event_key": event.event_key,
        "series_key": event.series_key,
        "title": event.title,
        "event_type": event.event_type,
        "start_at": event.start_at,
        "end_at": event.end_at,
        "date_precision": event.date_precision,
        "review_status": event.review_status,
        "recorded_at": event.created_at,
        "updated_at": event.updated_at,
    }


def _document_summary(version: DocumentVersion, document: Document, source: Source) -> dict[str, Any]:
    published_at = version.published_at or document.published_at
    public_metadata_fields = {
        "topics",
        "keywords",
        "topic_provenance",
        "research_object",
        "field_provenance",
        "published_online",
        "published_print",
        "replication_urls",
        "verification_status",
        "policy_material_kind",
        "journal_title",
        "authors",
        "author",
        "page",
        "pages",
        "paragraph",
        "method",
        "research_method",
        "sample_scope",
        "data_sources",
        "full_text_status",
        "translation_status",
        "claim_scope",
        "lifecycle_status",
        "effective_at",
        "amended_at",
        "repealed_at",
        "implementation_status",
        "version_label",
        "citation",
    }
    return {
        "document_id": document.id,
        "document_version_id": version.id,
        "version_no": version.version_no,
        "title": version.title,
        "doi": document.doi,
        "published_at": published_at,
        "published_at_precision": _published_at_precision(version),
        "observed_at": document.last_seen_at,
        "recorded_at": version.extracted_at,
        "updated_at": version.source_updated_at or document.last_seen_at,
        "language": version.language,
        "document_type": document.document_type,
        "source_id": source.id,
        "source_name": source.name,
        "source_type": source.source_type,
        "canonical_url": document.canonical_url,
        "discovery_url": document.discovery_url,
        "source_url": document.canonical_url or document.discovery_url,
        "abstract": version.abstract,
        "metadata": {
            key: value
            for key, value in (version.source_metadata or {}).items()
            if key in public_metadata_fields
        },
    }


def _claim_read(claim: EvidenceClaim) -> dict[str, Any]:
    return {
        "id": claim.id,
        "claim_kind": claim.claim_kind,
        "claimant": claim.claimant_name,
        "subject": claim.subject_text,
        "predicate": claim.predicate,
        "value_text": claim.value_text,
        "numeric_value": _number(claim.numeric_value),
        "unit": claim.unit,
        "time_scope": claim.time_scope,
        "comparison_key": claim.comparison_key,
        "position_summary": claim.position_summary,
        "evidence_locator": claim.evidence_locator,
        "review_status": claim.review_status,
    }


def _case_summary(db: Session, research_case: ResearchCase) -> dict[str, Any]:
    event_count = db.scalar(
        select(func.count())
        .select_from(ResearchCaseEvent)
        .join(ResearchEvent, ResearchEvent.id == ResearchCaseEvent.event_id)
        .where(
            ResearchCaseEvent.research_case_id == research_case.id,
            ResearchEvent.review_status == "reviewed",
        )
    )
    document_count = db.scalar(
        select(func.count(func.distinct(DocumentVersion.document_id)))
        .select_from(ResearchCaseDocument)
        .join(DocumentVersion, DocumentVersion.id == ResearchCaseDocument.document_version_id)
        .where(
            ResearchCaseDocument.research_case_id == research_case.id,
            or_(
                ResearchCaseDocument.document_version_id.in_(
                    select(DocumentEntity.document_version_id).where(
                        DocumentEntity.review_status == "confirmed"
                    )
                ),
                ResearchCaseDocument.document_version_id.in_(
                    select(EventMention.document_version_id).where(EventMention.review_status == "confirmed")
                ),
            ),
        )
    )
    latest_run = db.scalar(
        select(CapabilityRun)
        .where(CapabilityRun.research_case_id == research_case.id)
        .order_by(CapabilityRun.created_at.desc(), CapabilityRun.id.desc())
        .limit(1)
    )
    formal_output_count = (
        db.scalar(
            select(func.count(CapabilityRunTarget.id))
            .join(CapabilityRun, CapabilityRun.id == CapabilityRunTarget.capability_run_id)
            .where(CapabilityRun.research_case_id == research_case.id)
        )
        or 0
    )
    pending_review_count = (
        db.scalar(
            select(func.count(CapabilityRun.id)).where(
                CapabilityRun.research_case_id == research_case.id,
                CapabilityRun.status == "succeeded",
                ~CapabilityRun.id.in_(select(CapabilityRunTarget.capability_run_id)),
            )
        )
        or 0
    )
    return {
        "id": research_case.id,
        "brief": project_workflow.brief(research_case),
        "title": research_case.title,
        "research_question": research_case.research_question,
        "status": research_case.status,
        "scope": _public_case_scope(research_case.scope),
        "event_count": event_count or 0,
        "document_count": document_count or 0,
        "latest_run": _run_summary(latest_run) if latest_run is not None else None,
        "pending_review_count": pending_review_count,
        "formal_output_count": formal_output_count,
        "updated_at": research_case.updated_at,
    }


def _research_case_workflow(
    db: Session,
    research_case: ResearchCase,
    *,
    event_count: int,
    document_count: int,
    data_slice_count: int,
) -> dict[str, Any]:
    evidence_count = event_count + document_count + data_slice_count
    succeeded_run_count = (
        db.scalar(
            select(func.count())
            .select_from(CapabilityRun)
            .where(
                CapabilityRun.research_case_id == research_case.id,
                CapabilityRun.status == "succeeded",
            )
        )
        or 0
    )
    confirmed_writeback_count = (
        db.scalar(
            select(func.count())
            .select_from(CapabilityRunTarget)
            .join(CapabilityRun, CapabilityRun.id == CapabilityRunTarget.capability_run_id)
            .where(
                CapabilityRun.research_case_id == research_case.id,
                CapabilityRunTarget.target_type == "research_case",
                CapabilityRunTarget.target_key == str(research_case.id),
            )
        )
        or 0
    )
    question_done = bool(research_case.research_question.strip())
    evidence_done = evidence_count > 0
    run_done = succeeded_run_count > 0
    writeback_done = confirmed_writeback_count > 0
    project_root = f"#/projects/{research_case.id}"
    steps = [
        {
            "id": "question_confirmed",
            "label": "研究问题已确认",
            "state": "done" if question_done else "blocked",
            "count": 1 if question_done else 0,
        },
        {
            "id": "evidence_included",
            "label": "证据已纳入",
            "state": "done" if evidence_done else "blocked",
            "count": evidence_count,
        },
        {
            "id": "skill_run",
            "label": "Skill 已运行",
            "state": "done" if run_done else "ready" if evidence_done else "blocked",
            "count": succeeded_run_count,
        },
        {
            "id": "review_writeback",
            "label": "结果已审核写回",
            "state": "done" if writeback_done else "ready" if run_done else "blocked",
            "count": confirmed_writeback_count,
        },
        {
            "id": "output_available",
            "label": "研究产出可用",
            "state": "done" if writeback_done else "blocked",
            "count": confirmed_writeback_count,
        },
    ]
    if not question_done:
        next_action = {
            "type": "confirm_question",
            "label": "确认研究问题",
            "href": f"{project_root}/overview",
            "blocking_reasons": ["项目尚未登记研究问题。"],
        }
    elif not evidence_done:
        next_action = {
            "type": "add_evidence",
            "label": "纳入已确认材料或 reviewed 事件",
            "href": f"{project_root}/materials",
            "blocking_reasons": ["当前项目没有可进入正式研究闭环的证据。"],
        }
    elif not run_done:
        next_action = {
            "type": "run_skill",
            "label": "选择并运行适用 Skill",
            "href": f"{project_root}/runs",
            "blocking_reasons": [],
        }
    elif not writeback_done:
        next_action = {
            "type": "review_writeback",
            "label": "审核产物并确认写回",
            "href": f"{project_root}/outputs",
            "blocking_reasons": ["成功运行尚未由研究者确认写回项目。"],
        }
    else:
        next_action = {
            "type": "review_output",
            "label": "查看已确认研究产出",
            "href": f"{project_root}/outputs",
            "blocking_reasons": [],
        }
    return {
        "steps": steps,
        "counts": {
            "events": event_count,
            "documents": document_count,
            "data_slices": data_slice_count,
            "succeeded_runs": succeeded_run_count,
            "confirmed_writebacks": confirmed_writeback_count,
        },
        "next_action": next_action,
        "complete": writeback_done,
    }


def _run_summary(run: CapabilityRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "config_id": run.config_id,
        "research_case_id": run.research_case_id,
        "config_revision_id": run.config_revision_id,
        "schedule_id": run.schedule_id,
        "requested_by": run.requested_by,
        "status": run.status,
        "review_status": run.review_status,
        "execution_summary": run.execution_summary,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _capability_output_has_evidence(slug: str | None, output: dict[str, Any] | None) -> bool:
    payload = output or {}
    if slug == "country-brief":
        return bool(payload.get("structured_observation_count") or payload.get("event_count"))
    if slug == "event-evidence-matrix":
        return any(item.get("distinct_sources", 0) for item in payload.get("events") or [])
    if slug == "topic-digest":
        return bool(payload.get("materials"))
    return False


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _country_name(iso3: str) -> str:
    try:
        item = next(country for country in _country_catalog()["countries"] if country["iso3"] == iso3)
    except (HTTPException, StopIteration):
        return iso3
    return item["name_zh"]


@router.get("/countries/{iso3}/collection-status")
def get_country_collection_status(iso3: str, db: DbSession):
    from app.services.country_collection_status import country_collection_status

    _require_catalog_country(iso3.upper())
    return country_collection_status(db, iso3.upper(), _material_channel)


@router.get("/countries/{iso3}/research-trends")
def get_country_research_trends(iso3: str, db: DbSession, years: Annotated[int, Query(ge=1, le=5)] = 3):
    from app.services.research_trends import research_trends

    _require_catalog_country(iso3.upper())
    if years not in {1, 3, 5}:
        raise HTTPException(status_code=422, detail="years must be 1, 3 or 5")
    items = []
    while True:
        page = search_materials(
            db, country_iso3=iso3.upper(), channel="frontier_research", limit=500, offset=len(items)
        )
        items.extend(page["items"])
        if len(items) >= page["total"]:
            break
    from app.services.country_research_collection import collection_coverage

    return {
        "country_iso3": iso3.upper(),
        **research_trends(items, years, datetime.now(UTC).date()),
        "collection": collection_coverage(db) if iso3.upper() == "COD" else None,
    }


def _validated_country_references(db: Session, payload: AssistantQuestionPayload) -> list[dict]:
    if not payload.references:
        return []
    if payload.context.space != "country" or not payload.context.country_iso3:
        raise HTTPException(status_code=422, detail="selected references require a country conversation")
    iso3 = payload.context.country_iso3
    references = []
    for ref in payload.references:
        if ref.type == "document_version":
            item = get_material(ref.id, db)
            if item["review_status"] != "confirmed" or iso3 not in {c["iso3"] for c in item["countries"]}:
                raise HTTPException(status_code=403, detail="material is outside confirmed country scope")
            version = db.get(DocumentVersion, ref.id)
            metadata = version.source_metadata or {}
            if (
                metadata.get("access_scope") in {"restricted", "private", "anonymized_restricted"}
                or metadata.get("citation_right") == "denied"
            ):
                raise HTTPException(status_code=403, detail="material is restricted")
            fields = {"title": item["title"], "abstract": item.get("abstract")}
            source_name = item["source_name"]
            title = item["title"]
            url = item.get("canonical_url")
        elif ref.type == "event":
            event = db.get(ResearchEvent, ref.id)
            country = db.get(ResearchEntity, event.country_entity_id) if event else None
            if not event or event.review_status != "reviewed" or not country or country.canonical_key != iso3:
                raise HTTPException(status_code=403, detail="event is outside reviewed country scope")
            fields = {"title": event.title, "summary": event.summary}
            title, source_name, url = event.title, "已复核事件", None
        else:
            from app.services.country_data_catalog import data_permissions

            row = db.execute(
                select(StructuredObservation, StructuredObservationVersion, StructuredSnapshot)
                .join(
                    StructuredObservationVersion,
                    StructuredObservationVersion.observation_id == StructuredObservation.id,
                )
                .join(
                    StructuredSnapshotObservation,
                    StructuredSnapshotObservation.observation_version_id == StructuredObservationVersion.id,
                )
                .join(StructuredSnapshot, StructuredSnapshot.id == StructuredSnapshotObservation.snapshot_id)
                .where(StructuredObservationVersion.id == ref.id, StructuredSnapshot.id == ref.snapshot_id)
            ).one_or_none()
            if (
                not row
                or row[0].country_iso3 != iso3
                or not data_permissions(row[2].source_metadata)["citation"]["allowed"]
            ):
                raise HTTPException(status_code=403, detail="observation is outside permitted snapshot scope")
            observation, version, snapshot = row
            fields = {
                "value": str(version.value) if version.value is not None else "缺失",
                "period": str(observation.period),
                "unit": version.unit,
            }
            title = observation.indicator_code or observation.metric_code
            source_name, url = "结构化来源观测", version.source_url
        if ref.quote is not None:
            value = fields.get(ref.field)
            if not value or ref.start is None or value[ref.start : ref.start + len(ref.quote)] != ref.quote:
                raise HTTPException(
                    status_code=422, detail="excerpt does not match the permitted source field and location"
                )
            text_value = ref.quote
        else:
            text_value = "\n".join(str(value) for value in fields.values() if value is not None)
        references.append(
            {
                **ref.model_dump(),
                "title": title,
                "source_name": source_name,
                "source_url": url,
                "text": text_value,
                "published_at_precision": item.get("published_at_precision", "unknown")
                if ref.type == "document_version"
                else "unknown",
                "published_at": str(item["published_at"])
                if ref.type == "document_version" and item.get("published_at")
                else None,
                "observed_at": str(item["observed_at"])
                if ref.type == "document_version" and item.get("observed_at")
                else None,
            }
        )
    return references
