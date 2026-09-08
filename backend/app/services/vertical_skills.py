from __future__ import annotations

import io
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from pypdf import PdfReader
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    CapabilityRun,
    Document,
    DocumentEntity,
    DocumentVersion,
    EventEntity,
    EventMention,
    EventRelation,
    EvidenceClaim,
    FieldMaterial,
    ResearchCase,
    ResearchCaseDocument,
    ResearchCaseEvent,
    ResearchEntity,
    ResearchEvent,
    Source,
    StructuredDataset,
    StructuredObservation,
    StructuredObservationVersion,
)
from app.services import project_research as project_workflow
from app.services.agent_runtime import AgentRuntimeError
from app.services.country_agent import get_structured_agent_runtime
from app.services.field_materials import resolve_field_material_path
from app.services.project_candidates import adopted_observations
from app.services.source_probe import ProbeError, SafeHttpClient

DISCIPLINES = (
    "政治治理",
    "经济贸易",
    "历史",
    "地理环境",
    "社会文化",
    "语言与对象国来源",
)


def _confirmed_document_version_clause() -> Any:
    return or_(
        select(EventMention.document_version_id)
        .where(
            EventMention.document_version_id == DocumentVersion.id,
            EventMention.review_status == "confirmed",
        )
        .exists(),
        select(DocumentEntity.document_version_id)
        .where(
            DocumentEntity.document_version_id == DocumentVersion.id,
            DocumentEntity.review_status == "confirmed",
        )
        .exists(),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _country(session: Session, iso3: str) -> ResearchEntity | None:
    return session.scalar(
        select(ResearchEntity).where(
            ResearchEntity.entity_type == "country",
            ResearchEntity.canonical_key == iso3,
        )
    )


def _event_scope(
    session: Session,
    research_case_id: int | None,
    config: dict[str, Any],
    *,
    policy_only: bool = False,
) -> list[ResearchEvent]:
    requested = [int(item) for item in config.get("event_ids") or config.get("policy_event_ids") or []]
    statement = select(ResearchEvent).where(ResearchEvent.review_status == "reviewed")
    research_case_id = research_case_id or config.get("research_case_id")
    if research_case_id:
        allowed = project_workflow.adopted_mention_ids(session, int(research_case_id))
        statement = statement.where(
            ResearchEvent.id.in_(select(EventMention.event_id).where(EventMention.id.in_(allowed)))
        )
    if policy_only:
        statement = statement.where(ResearchEvent.event_type == "policy")
    if requested:
        statement = statement.where(ResearchEvent.id.in_(requested))
    elif research_case_id:
        statement = statement.join(ResearchCaseEvent, ResearchCaseEvent.event_id == ResearchEvent.id).where(
            ResearchCaseEvent.research_case_id == research_case_id
        )
    else:
        iso3 = str(config.get("country_iso3") or "").upper()
        country = _country(session, iso3)
        if country is None:
            return []
        statement = statement.where(ResearchEvent.country_entity_id == country.id)
    date_from = config.get("date_from")
    date_to = config.get("date_to")
    if date_from:
        statement = statement.where(ResearchEvent.start_at >= datetime.fromisoformat(str(date_from)))
    if date_to:
        statement = statement.where(ResearchEvent.start_at <= datetime.fromisoformat(str(date_to)))
    return list(session.scalars(statement.order_by(ResearchEvent.start_at, ResearchEvent.id).limit(200)))


def country_brief(session: Session, config: dict[str, Any]) -> dict[str, Any]:
    iso3 = str(config.get("country_iso3") or "").upper()
    if len(iso3) != 3:
        raise ValueError("country-brief config requires country_iso3")
    country = _country(session, iso3)
    events = _event_scope(session, None, config) if country else []
    observations = list(
        session.execute(
            select(StructuredObservation, StructuredObservationVersion, StructuredDataset)
            .join(
                StructuredObservationVersion,
                (StructuredObservationVersion.observation_id == StructuredObservation.id)
                & (StructuredObservationVersion.version_no == StructuredObservation.latest_version_no),
            )
            .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
            .where(StructuredObservation.country_iso3 == iso3)
            .order_by(StructuredObservation.period.desc())
            .limit(800)
        )
    )
    locators = {}
    if config.get("research_case_id"):
        observations, locators = adopted_observations(session, int(config["research_case_id"]), [iso3])
    event_ids = [event.id for event in events]
    material_rows = []
    claim_rows = []
    if event_ids:
        material_rows = list(
            session.execute(
                select(EventMention, DocumentVersion, Document, Source)
                .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    EventMention.event_id.in_(event_ids),
                    EventMention.review_status == "confirmed",
                )
            )
        )
        claim_rows = list(
            session.execute(
                select(EvidenceClaim, EventMention)
                .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
                .where(
                    EventMention.event_id.in_(event_ids),
                    EventMention.review_status == "confirmed",
                    EvidenceClaim.review_status == "confirmed",
                )
            )
        )
    case_id = config.get("research_case_id")
    if case_id:
        allowed = project_workflow.adopted_mention_ids(session, int(case_id))
        material_rows = [row for row in material_rows if row[0].id in allowed]
        claim_rows = [row for row in claim_rows if row[1].id in allowed]
    themes: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "历史",
            "制度政治",
            "经济贸易",
            "社会",
            "地理",
            "战略议题",
        )
    }
    for observation, version, dataset in observations:
        code = str(observation.indicator_code or "").lower()
        theme = "经济贸易"
        if any(token in code for token in ("pop", "health", "education", "poverty")):
            theme = "社会"
        elif any(token in code for token in ("area", "land", "forest", "geo")):
            theme = "地理"
        themes[theme].append(
            {
                "kind": "structured_observation",
                "locator": locators.get(version.id),
                "value_text": str(version.value) if version.value is not None else None,
                "missing_reason": version.missing_reason,
                "indicator_code": observation.indicator_code,
                "period": observation.period,
                "value": float(version.value) if version.value is not None else None,
                "unit": version.unit,
                "dataset_key": dataset.dataset_key,
                "source_url": version.source_url,
            }
        )
    for event in events:
        theme = "制度政治" if event.event_type == "policy" else "战略议题"
        themes[theme].append(
            {
                "kind": "reviewed_event",
                "event_id": event.id,
                "title": event.title,
                "start_at": _iso(event.start_at),
                "summary": event.summary,
            }
        )
    conflicts: list[dict[str, Any]] = []
    grouped_claims: dict[str, list[EvidenceClaim]] = defaultdict(list)
    for claim, _ in claim_rows:
        grouped_claims[claim.comparison_key].append(claim)
    for key, items in grouped_claims.items():
        values = {item.value_text.strip() for item in items if item.value_text.strip()}
        if len(values) > 1:
            conflicts.append({"comparison_key": key, "values": sorted(values), "claim_count": len(items)})
    source_names = sorted({source.name for _, _, _, source in material_rows})
    return {
        "type": "country_brief",
        "schema_version": "2.1",
        "country_iso3": iso3,
        "country_name": country.canonical_name if country else iso3,
        "themes": [
            {"name": name, "items": items[:40], "count": len(items)} for name, items in themes.items()
        ],
        "event_count": len(events),
        "structured_observation_count": len(observations),
        "confirmed_material_count": len(material_rows),
        "source_chain": [
            {"kind": "structured_observation", "count": len(observations)},
            {"kind": "reviewed_event", "count": len(events)},
            {"kind": "confirmed_material", "count": len(material_rows), "sources": source_names},
        ],
        "conflicts": conflicts,
        "evidence_gaps": [name for name, items in themes.items() if not items],
        "suggested_next_skill": "S03" if events else "S09",
        "method_note": (
            "六个主题只组织已入库结构化观测、reviewed 事件和 confirmed 材料；不以数量统计冒充画像。"
        ),
    }


def _extract_pdf(body: bytes) -> list[dict[str, str]]:
    reader = PdfReader(io.BytesIO(body))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append({"locator": f"page:{index}", "text": text})
    return pages


def _extract_field_material(material: FieldMaterial) -> list[dict[str, str]]:
    path = resolve_field_material_path(material.storage_key, expected_sha256=material.sha256)
    if material.content_type == "application/pdf":
        return _extract_pdf(path.read_bytes())
    if material.content_type in {"text/plain", "text/markdown"}:
        return [
            {"locator": f"line:{index}", "text": line.strip()}
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if line.strip()
        ]
    return []


def bilingual_policy_verification(
    session: Session,
    research_case_id: int | None,
    config: dict[str, Any],
    *,
    safe_client: SafeHttpClient | None = None,
) -> dict[str, Any]:
    case_id = research_case_id or config.get("research_case_id")
    source_language = str(config.get("source_language") or "fr")
    target_language = str(config.get("target_language") or "zh-CN")
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    for version_id in {int(item) for item in config.get("document_version_ids") or []}:
        row = session.execute(
            select(DocumentVersion, Document, Source)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(Source, Source.id == Document.source_id)
            .where(DocumentVersion.id == version_id)
        ).one_or_none()
        if row is None:
            errors.append(f"文档版本 #{version_id} 不存在。")
            continue
        version, document, source = row
        if (
            case_id
            and session.scalar(
                select(ResearchCaseDocument.id).where(
                    ResearchCaseDocument.research_case_id == int(case_id),
                    ResearchCaseDocument.document_version_id == version.id,
                )
            )
            is None
        ):
            errors.append(f"文档版本 #{version_id} 不属于当前专题。")
            continue
        if case_id and not project_workflow.adopted_document(
            session, project_workflow.adoption_decisions(session, int(case_id)), version.id
        ):
            errors.append(f"文档版本 #{version_id} 未采用或当前引用许可不允许。")
            continue
        official_source_types = {"official", "government", "government_or_international"}
        if (
            not document.canonical_url
            or source.source_type not in official_source_types
            or source.authority_level != "official"
        ):
            errors.append(f"文档版本 #{version_id} 没有已核验官方 canonical URL。")
            continue
        try:
            resource = (safe_client or SafeHttpClient()).fetch(document.canonical_url)
            if resource.body.startswith(b"%PDF-"):
                pages = _extract_pdf(resource.body)
            else:
                text = resource.body.decode("utf-8")
                pages = [{"locator": "body", "text": text.strip()}] if text.strip() else []
        except (ProbeError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"{version.title}：安全获取或原文提取失败（{exc}）")
            continue
        sources.append(
            {
                "source_id": f"document-version:{version.id}",
                "title": version.title or document.title,
                "institution": source.organization_name or source.name,
                "published_at": _iso(version.published_at or document.published_at),
                "source_url": document.canonical_url,
                "sha256": resource.sha256,
                "pages": pages,
            }
        )
    requested_upload_ids = {
        int(item.get("upload_id"))
        for item in config.get("file_refs") or []
        if isinstance(item, dict) and str(item.get("upload_id") or "").isdigit()
    }
    if requested_upload_ids:
        if not case_id:
            raise ValueError("bilingual-policy-verification file_refs require research_case_id")
        materials = list(
            session.scalars(
                select(FieldMaterial).where(
                    FieldMaterial.id.in_(requested_upload_ids),
                    FieldMaterial.research_case_id == int(case_id),
                    FieldMaterial.authorization_confirmed.is_(True),
                )
            )
        )
        allowed_materials = project_workflow.adopted_field_material_ids(session, int(case_id))
        for material in materials:
            if material.id not in allowed_materials:
                continue
            pages = _extract_field_material(material)
            sources.append(
                {
                    "source_id": f"field-material:{material.id}",
                    "title": material.title,
                    "institution": "用户授权材料",
                    "published_at": material.captured_on.isoformat() if material.captured_on else None,
                    "source_url": None,
                    "sha256": material.sha256,
                    "pages": pages,
                }
            )
        missing = requested_upload_ids - {item.id for item in materials}
        if missing:
            errors.append(f"{len(missing)} 个文件不属于当前专题或未授权。")
    for index, item in enumerate(config.get("source_urls") or [], start=1):
        if not isinstance(item, dict):
            errors.append(f"临时链接 #{index} 格式无效。")
            continue
        url = str(item.get("url") or "").strip()
        if not url or item.get("access_authorized") is not True:
            errors.append(f"临时链接 #{index} 未确认访问与处理授权。")
            continue
        try:
            resource = (safe_client or SafeHttpClient()).fetch(url)
            if resource.body.startswith(b"%PDF-"):
                pages = _extract_pdf(resource.body)
            else:
                text = resource.body.decode("utf-8")
                pages = [{"locator": "body", "text": text.strip()}] if text.strip() else []
        except (ProbeError, UnicodeDecodeError, ValueError) as exc:
            errors.append(f"临时链接 #{index}：安全获取或原文提取失败（{exc}）")
            continue
        sources.append(
            {
                "source_id": f"user-link:{index}",
                "title": str(item.get("title") or f"用户授权链接 #{index}"),
                "institution": "用户提供链接",
                "published_at": None,
                "source_url": resource.final_url,
                "sha256": resource.sha256,
                "verification_status": "user_link_unreviewed",
                "pages": pages,
            }
        )
    if not sources:
        return {
            "type": "bilingual_policy_verification",
            "result_status": "candidate",
            "segments": [],
            "source_chain": [],
            "evidence_gaps": errors or ["没有可提取的官方原文或授权文件。"],
            "error_code": "insufficient_data",
            "method_note": "未保存外部原文或原始响应。",
            "processing_basis": ["专题范围", "用户授权", "SafeHttpClient 安全获取"],
        }
    runtime = get_structured_agent_runtime(get_settings())
    if runtime is None:
        return {
            "type": "bilingual_policy_verification",
            "result_status": "candidate",
            "segments": [],
            "source_chain": [
                {key: value for key, value in item.items() if key != "pages"} for item in sources
            ],
            "evidence_gaps": [*errors, "已冻结原文，但当前没有可用的受控模型运行时。"],
            "error_code": "agent_runtime_required",
            "method_note": "模型不可用时不生成伪译文。",
            "processing_basis": ["冻结原文", "来源与定位保留", "缺少受控译文运行时"],
        }
    model_sources = []
    lookup: dict[tuple[str, str], str] = {}
    for item in sources:
        selected_pages = item["pages"][:12]
        model_sources.append(
            {
                "source_id": item["source_id"],
                "title": item["title"],
                "pages": selected_pages,
            }
        )
        for page in selected_pages:
            lookup[(item["source_id"], page["locator"])] = page["text"]
    try:
        draft = runtime.translate_policy_segments(
            source_language=source_language,
            target_language=target_language,
            sources=model_sources,
        )
    except AgentRuntimeError as exc:
        return {
            "type": "bilingual_policy_verification",
            "result_status": "candidate",
            "segments": [],
            "source_chain": [
                {key: value for key, value in item.items() if key != "pages"} for item in sources
            ],
            "evidence_gaps": [*errors, str(exc)],
            "error_code": "agent_runtime_required",
            "method_note": "原文已安全提取，但没有在模型失败时伪造译文。",
            "processing_basis": ["冻结原文", "受控译文调用失败", "禁止伪造译文"],
        }
    segments = []
    for item in draft.segments:
        original = lookup.get((item.source_id, item.locator), "")
        if not original or item.original_excerpt not in original:
            continue
        segments.append(item.model_dump())
    glossary = []
    ambiguities = []
    for segment in segments:
        for term in segment.get("key_terms") or []:
            if term not in glossary:
                glossary.append(term)
        ambiguities.extend(
            {
                "source_id": segment["source_id"],
                "locator": segment["locator"],
                "note": note,
            }
            for note in segment.get("ambiguities") or []
        )
    return {
        "type": "bilingual_policy_verification",
        "result_status": "candidate",
        "source_language": source_language,
        "target_language": target_language,
        "segments": segments,
        "institutions": draft.institutions,
        "key_provisions": draft.key_provisions,
        "glossary": glossary,
        "ambiguity_items": ambiguities,
        "source_chain": [{key: value for key, value in item.items() if key != "pages"} for item in sources],
        "evidence_gaps": [
            *errors,
            *draft.limitations,
            *([] if segments else ["模型输出未通过原文反向定位校验。"]),
        ],
        "suggested_next_skill": "S03",
        "processing_basis": [
            "只处理冻结证据包",
            "原文片段必须反向定位",
            "机构、术语与歧义均为候选结果",
        ],
        "method_note": "只保留通过原文子串与页码反向定位校验的对照片段；不保存外部全文。",
    }


def policy_dynamics(
    session: Session,
    config: dict[str, Any],
    *,
    config_id: int | None = None,
) -> dict[str, Any]:
    iso3 = str(config.get("country_iso3") or "").upper()
    country = _country(session, iso3)
    if country is None:
        return {"type": "policy_dynamics", "country_iso3": iso3, "materials": [], "events": []}
    events = _event_scope(session, None, config, policy_only=True)
    event_ids = [event.id for event in events]
    statement = (
        select(EventMention, DocumentVersion, Document, Source)
        .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            EventMention.event_id.in_(event_ids),
            EventMention.review_status == "confirmed",
        )
    )
    source_ids = [int(item) for item in config.get("source_ids") or []]
    if source_ids:
        statement = statement.where(Source.id.in_(source_ids))
    keywords = [str(item).strip().lower() for item in config.get("keywords") or [] if str(item).strip()]
    rows = list(session.execute(statement.order_by(Document.published_at.desc(), DocumentVersion.id.desc())))
    if config.get("research_case_id"):
        allowed = project_workflow.adopted_mention_ids(session, int(config["research_case_id"]))
        rows = [row for row in rows if row[0].id in allowed]
    if keywords:
        rows = [
            row
            for row in rows
            if any(
                word in " ".join((row[1].title, row[0].mention_summary or "")).lower() for word in keywords
            )
        ]
    materials = [
        {
            "document_version_id": version.id,
            "document_id": document.id,
            "content_sha256": version.content_sha256,
            "title": version.title or document.title,
            "source": source.name,
            "source_id": source.id,
            "source_url": document.canonical_url or document.discovery_url,
            "published_at": _iso(version.published_at or document.published_at),
            "observed_at": _iso(document.last_seen_at),
            "version_no": version.version_no,
            "mention_summary": mention.mention_summary,
            "evidence_locator": mention.evidence_locator,
        }
        for mention, version, document, source in rows
    ]
    previous = None
    if config_id:
        previous = session.scalar(
            select(CapabilityRun)
            .where(CapabilityRun.config_id == config_id, CapabilityRun.status == "succeeded")
            .order_by(CapabilityRun.id.desc())
            .limit(1)
        )
    prior = (
        {
            item.get("document_id", f"version:{item.get('document_version_id')}"): item
            for item in (previous.output or {}).get("materials", [])
        }
        if previous
        else {}
    )
    if config.get("research_case_id"):
        decisions = project_workflow.adoption_decisions(session, int(config["research_case_id"]))
        prior = {
            key: item
            for key, item in prior.items()
            if project_workflow.adopted_document(session, decisions, item.get("document_version_id", 0))
        }
    current = {item["document_id"]: item for item in materials}
    added = [item for key, item in current.items() if key not in prior]
    removed = [item for key, item in prior.items() if key not in current]
    updated = [
        {"before": prior[key], "after": item, "body_diff_available": False}
        for key, item in current.items()
        if key in prior
        and (item["content_sha256"], item["version_no"])
        != (prior[key].get("content_sha256"), prior[key].get("version_no"))
    ]
    return {
        "type": "policy_dynamics",
        "country_iso3": iso3,
        "events": [{"id": item.id, "title": item.title, "start_at": _iso(item.start_at)} for item in events],
        "materials": materials,
        "changes": {"added": added, "updated": updated, "removed": removed},
        "comparison_run_id": previous.id if previous else None,
        "reading_queue": [item["document_version_id"] for item in materials],
        "evidence_gaps": ([] if rows else ["当前没有符合筛选条件的 confirmed 政策材料。"]),
        "suggested_next_skill": "S02",
        "method_note": "新增、更新和退出仅与同配置上次成功快照比较；无正文时只比较哈希和元数据。",
    }


def event_timeline(session: Session, research_case_id: int | None, config: dict[str, Any]) -> dict[str, Any]:
    research_case_id = research_case_id or config.get("research_case_id")
    allowed = (
        project_workflow.adopted_mention_ids(session, int(research_case_id)) if research_case_id else None
    )
    events = _event_scope(session, research_case_id, config)
    event_ids = [event.id for event in events]
    entity_rows = (
        list(
            session.execute(
                select(EventEntity, ResearchEntity)
                .join(ResearchEntity, ResearchEntity.id == EventEntity.entity_id)
                .where(EventEntity.event_id.in_(event_ids))
            )
        )
        if event_ids
        else []
    )
    entities: dict[int, list[dict[str, str]]] = defaultdict(list)
    for link, entity in entity_rows:
        entities[link.event_id].append({"role": link.role, "name": entity.canonical_name})
    items = []
    conflict_groups = []
    for event in events:
        mentions = list(
            session.execute(
                select(EventMention, DocumentVersion, Document, Source)
                .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(EventMention.event_id == event.id, EventMention.review_status == "confirmed")
            )
        )
        if allowed is not None:
            mentions = [row for row in mentions if row[0].id in allowed]
        claims = list(
            session.scalars(
                select(EvidenceClaim)
                .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
                .where(
                    EventMention.event_id == event.id,
                    EvidenceClaim.review_status == "confirmed",
                )
            )
        )
        mention_ids = {row[0].id for row in mentions}
        claims = [claim for claim in claims if claim.event_mention_id in mention_ids]
        grouped: dict[str, list[EvidenceClaim]] = defaultdict(list)
        for claim in claims:
            grouped[claim.comparison_key].append(claim)
        for key, group in grouped.items():
            values = {item.value_text for item in group}
            if len(values) > 1:
                conflict_groups.append(
                    {"event_id": event.id, "comparison_key": key, "values": sorted(values)}
                )
        items.append(
            {
                "event_id": event.id,
                "title": event.title,
                "start_at": _iso(event.start_at),
                "end_at": _iso(event.end_at),
                "date_precision": event.date_precision,
                "summary": event.summary,
                "place": next(
                    (item["name"] for item in entities[event.id] if item["role"] == "location"),
                    (event.details or {}).get("place"),
                ),
                "actors": [item["name"] for item in entities[event.id] if item["role"] == "actor"],
                "actions": sorted({claim.predicate for claim in claims if claim.predicate}),
                "materials": [
                    {
                        "document_version_id": version.id,
                        "title": version.title or document.title,
                        "source": source.name,
                        "source_url": document.canonical_url or document.discovery_url,
                        "summary": mention.mention_summary,
                        "locator": mention.evidence_locator,
                    }
                    for mention, version, document, source in mentions
                ],
                "confirmed_source_mentions": len(mentions),
            }
        )
    relations = (
        list(
            session.scalars(
                select(EventRelation).where(
                    or_(
                        EventRelation.source_event_id.in_(event_ids),
                        EventRelation.target_event_id.in_(event_ids),
                    )
                )
            )
        )
        if event_ids
        else []
    )
    return {
        "type": "event_timeline",
        "items": items,
        "conflict_groups": conflict_groups,
        "merge_candidates": [
            {
                "source_event_id": item.source_event_id,
                "target_event_id": item.target_event_id,
                "relation_type": item.relation_type,
                "note": item.note,
            }
            for item in relations
            if item.relation_type == "same_series"
        ],
        "relations": [
            {
                "source_event_id": item.source_event_id,
                "target_event_id": item.target_event_id,
                "relation_type": item.relation_type,
                "note": item.note,
            }
            for item in relations
        ],
        "evidence_gaps": [
            f"事件 #{item['event_id']} 没有 confirmed 来源。"
            for item in items
            if not item["confirmed_source_mentions"]
        ],
        "suggested_next_skill": "S06",
        "method_note": "同 event_id 材料归组；跨事件只列合并候选，不自动改库或覆盖冲突。",
    }


def _organize_transcript_text(text: str) -> tuple[str, list[dict[str, str]]]:
    organized = text.strip()
    edits: list[dict[str, str]] = []
    pause_pattern = re.compile(r"(?:\[(?:停顿|pause)\]|（(?:停顿|pause)）)", re.IGNORECASE)
    if pause_pattern.search(organized):
        organized = pause_pattern.sub("", organized)
        edits.append({"type": "口头停顿", "note": "移除明确标注的停顿符号"})
    filler_pattern = re.compile(
        r"(^|[，,。.!！?？]\s*)(嗯+|呃+|额+|uh+|um+)(?=[，,。.!！?？\s]|$)",
        re.IGNORECASE,
    )
    if filler_pattern.search(organized):
        organized = filler_pattern.sub(lambda match: match.group(1), organized)
        edits.append({"type": "语气词", "note": "移除可明确识别且不承载语义的语气词"})
    repeated_word_pattern = re.compile(r"\b([A-Za-z]{1,20})(?:\s+\1){1,}\b", re.IGNORECASE)
    if repeated_word_pattern.search(organized):
        organized = repeated_word_pattern.sub(r"\1", organized)
        edits.append({"type": "重复", "note": "合并英语转写中的连续同词重复"})
    organized = re.sub(r"[ \t]{2,}", " ", organized).strip(" ，,")
    return organized, edits


def _transcript_themes(text: str) -> list[str]:
    lowered = text.lower()
    rules = {
        "政策治理": ("政策", "政府", "治理", "policy", "government"),
        "矿产与经济": ("矿", "钴", "铜", "贸易", "经济", "mining", "cobalt", "trade"),
        "安全与冲突": ("冲突", "安全", "武装", "conflict", "security"),
        "社会与社区": ("社区", "民众", "家庭", "社会", "community", "social"),
    }
    return [name for name, tokens in rules.items() if any(token in lowered for token in tokens)]


def field_material_organizer(
    session: Session, research_case_id: int | None, config: dict[str, Any]
) -> dict[str, Any]:
    case_id = research_case_id or config.get("research_case_id")
    if not case_id:
        raise ValueError("field-material-organizer requires research_case_id")
    requested = [
        int(item["upload_id"])
        for item in config.get("file_refs") or []
        if isinstance(item, dict) and str(item.get("upload_id") or "").isdigit()
    ]
    allowed_materials = project_workflow.adopted_field_material_ids(session, int(case_id))
    materials = {
        item.id: item
        for item in session.scalars(
            select(FieldMaterial).where(
                FieldMaterial.id.in_(set(requested) & allowed_materials),
                FieldMaterial.research_case_id == int(case_id),
                FieldMaterial.authorization_confirmed.is_(True),
            )
        )
    }
    segments: list[dict[str, Any]] = []
    file_refs = []
    for material_id in requested:
        material = materials.get(material_id)
        if material is None:
            continue
        pages = _extract_field_material(material)
        file_refs.append(
            {
                "upload_id": material.id,
                "title": material.title,
                "filename": material.original_filename,
                "content_type": material.content_type,
                "privacy": material.privacy_level,
                "evidence_status": material.evidence_status,
                "captured_on": material.captured_on.isoformat() if material.captured_on else None,
                "sha256": material.sha256,
                "method": material.method_note or "用户提供",
            }
        )
        if material.content_type.startswith("image/"):
            continue
        for page in pages[:80]:
            match = re.match(
                r"^(?:\[(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?(?:(?P<speaker>[^:：]{1,30})[:：]\s*)?(?P<text>.+)$",
                page["text"],
            )
            if not match:
                continue
            verbatim = match.group("text").strip()[:1200]
            organized, edits = _organize_transcript_text(verbatim)
            themes = _transcript_themes(verbatim)
            segments.append(
                {
                    "excerpt_id": f"upload-{material.id}-{page['locator']}",
                    "locator": page["locator"],
                    "timestamp": match.group("time"),
                    "speaker": match.group("speaker") or "未标注角色",
                    "verbatim_text": verbatim,
                    "organized_text": organized,
                    "text": organized,
                    "edits": edits,
                    "method": material.method_note or material.material_type,
                    "themes": themes,
                    "privacy": material.privacy_level,
                    "source_ref": f"field-material:{material.id}",
                }
            )
    for index, item in enumerate(config.get("transcript_items") or [], start=1):
        if isinstance(item, dict):
            source_ref = str(item.get("source_ref") or "")
            if source_ref.startswith("field-material:"):
                material_id = source_ref.removeprefix("field-material:")
                if not material_id.isdigit() or int(material_id) not in allowed_materials:
                    continue
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            verbatim = str(item["text"]).strip()
            organized, edits = _organize_transcript_text(verbatim)
            segments.append(
                {
                    "excerpt_id": f"provided-{index}",
                    "locator": item.get("source_ref") or f"provided:{index}",
                    "timestamp": item.get("timestamp"),
                    "speaker": item.get("speaker") or "未标注角色",
                    "verbatim_text": verbatim,
                    "organized_text": organized,
                    "text": organized,
                    "edits": edits,
                    "method": item.get("method") or "用户提供转写",
                    "themes": item.get("themes") or _transcript_themes(verbatim),
                    "privacy": item.get("privacy") or config.get("privacy_mode", "restricted"),
                    "source_ref": item.get("source_ref"),
                }
            )
    return {
        "type": "field_material_organization",
        "result_status": "candidate",
        "segments": segments,
        "excerpts": segments,
        "viewpoint_cards": [
            {
                "segment_id": item["excerpt_id"],
                "speaker": item["speaker"],
                "summary": item["organized_text"],
                "themes": item["themes"],
                "source_ref": item["source_ref"],
                "locator": item["locator"],
            }
            for item in segments
        ],
        "file_refs": file_refs,
        "evidence_gaps": [
            *([] if segments else ["当前没有可定位转写文本；图片只登记文件与元数据。"]),
            *(
                [f"{len(requested) - len(file_refs)} 个引用跨专题、未授权或不存在。"]
                if len(requested) != len(file_refs)
                else []
            ),
        ],
        "suggested_next_skill": "S06",
        "processing_basis": [
            "逐字稿保持原样并绑定行号或页码",
            "只清理明确停顿、语气词和连续英语同词重复",
            "主题与观点均为候选，需研究者复核",
        ],
        "method_note": (
            "TXT/Markdown/PDF 的逐字稿不被覆盖；整理稿只做保守语言清理。"
            "图片不做 OCR，不接收音频、不自动转写或推断身份。"
        ),
    }


def _disciplines_for(text: str, source: Source) -> list[dict[str, str]]:
    lowered = text.lower()
    rules = {
        "政治治理": ("政策", "政府", "治理", "policy", "government", "election"),
        "经济贸易": ("经济", "贸易", "矿产", "钴", "铜", "trade", "mining", "cobalt"),
        "历史": ("历史", "殖民", "history", "colonial"),
        "地理环境": ("地理", "环境", "气候", "河流", "environment", "climate"),
        "社会文化": ("社会", "文化", "教育", "健康", "social", "culture"),
    }
    matches: list[dict[str, str]] = []
    if source.primary_language and source.primary_language.lower() not in {"zh", "zh-cn", "en"}:
        matches.append(
            {
                "discipline": "语言与对象国来源",
                "basis": f"来源主要语言为 {source.primary_language}",
            }
        )
    for discipline, tokens in rules.items():
        matched = [token for token in tokens if token in lowered]
        if matched:
            matches.append(
                {
                    "discipline": discipline,
                    "basis": f"标题或摘要命中：{'、'.join(matched[:4])}",
                }
            )
    return matches


def interdisciplinary_evidence(
    session: Session, research_case_id: int | None, config: dict[str, Any]
) -> dict[str, Any]:
    case_id = int(research_case_id or config.get("research_case_id") or 0)
    if not case_id:
        raise ValueError("interdisciplinary-evidence requires research_case_id")
    selected_ids = {int(item) for item in config.get("document_version_ids") or []}
    rows = _topic_documents(session, case_id, selected_ids)
    matrix: dict[str, dict[str, list[dict[str, Any]]]] = {
        discipline: {key: [] for key in ("support", "refute", "background", "to_verify")}
        for discipline in DISCIPLINES
    }
    unclassified = []
    for link, version, document, source in rows:
        item = {
            "document_version_id": version.id,
            "title": version.title or document.title,
            "source": source.name,
            "source_url": document.canonical_url or document.discovery_url,
            "usage_type": link.usage_type,
            "version_no": version.version_no,
        }
        classifications = _disciplines_for(" ".join((item["title"], version.abstract or "")), source)
        item["classification_candidates"] = classifications
        if not classifications:
            unclassified.append(item)
        else:
            for classification in classifications:
                matrix[classification["discipline"]][link.usage_type].append(item)
    coverage = [name for name, groups in matrix.items() if any(groups.values())]
    return {
        "type": "interdisciplinary_evidence",
        "result_status": "candidate",
        "research_case_id": case_id,
        "matrix": [{"discipline": name, "evidence": groups} for name, groups in matrix.items()],
        "unclassified": unclassified,
        "perspective_differences": [
            {"discipline": name, "note": "同学科同时存在支持与反驳材料，需人工核对概念和语境。"}
            for name, groups in matrix.items()
            if groups["support"] and groups["refute"]
        ],
        "discipline_gaps": [name for name in DISCIPLINES if name not in coverage],
        "evidence_gaps": [
            *([f"{len(unclassified)} 份材料待研究者确认学科分类。"] if unclassified else []),
            *([] if rows else ["专题内没有可组织材料。"]),
        ],
        "suggested_next_skill": "S08",
        "processing_basis": ["语言字段", "标题和摘要的公开关键词", "专题内材料用途"],
        "method_note": (
            "一份材料可同时命中多个学科标签；标签和专题用途都是候选结果，不自动升级 reviewed/confirmed 状态。"
        ),
    }


def _topic_documents(
    session: Session,
    research_case_id: int,
    selected_ids: set[int] | None = None,
) -> list[tuple[ResearchCaseDocument, DocumentVersion, Document, Source]]:
    statement = (
        select(ResearchCaseDocument, DocumentVersion, Document, Source)
        .join(DocumentVersion, DocumentVersion.id == ResearchCaseDocument.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            ResearchCaseDocument.research_case_id == research_case_id,
            _confirmed_document_version_clause(),
        )
    )
    if selected_ids:
        statement = statement.where(DocumentVersion.id.in_(selected_ids))
    decisions = project_workflow.adoption_decisions(session, research_case_id)
    return [
        row
        for row in session.execute(statement)
        if project_workflow.adopted_document(session, decisions, row[1].id)
    ]


def material_relevance_ranking(
    session: Session, research_case_id: int | None, config: dict[str, Any]
) -> dict[str, Any]:
    case_id = int(research_case_id or config.get("research_case_id") or 0)
    research_case = session.get(ResearchCase, case_id) if case_id else None
    if research_case is None:
        raise ValueError("material-relevance-ranking requires research_case_id")
    query = str(config.get("query") or research_case.research_question or research_case.title).strip()
    keywords = [str(item).strip().lower() for item in config.get("keywords") or [] if str(item).strip()]
    if not keywords:
        keywords = [item.lower() for item in re.findall(r"[\w\u4e00-\u9fff]{2,}", query)[:12]]
    default_weights = {
        "topic_relevance": 0.30,
        "country_match": 0.15,
        "time_coverage": 0.10,
        "source_type": 0.10,
        "originality": 0.15,
        "deduplication": 0.10,
        "existing_evidence": 0.10,
    }
    weights = {**default_weights, **(config.get("weights") or {})}
    weight_total = sum(max(float(value), 0.0) for value in weights.values()) or 1.0
    weights = {key: max(float(value), 0.0) / weight_total for key, value in weights.items()}
    rows = _topic_documents(session, case_id)
    seen_hashes: set[str] = set()
    ranked = []
    for link, version, document, source in rows:
        text = " ".join((version.title or document.title, version.abstract or "")).lower()
        matched = [token for token in keywords if token in text]
        is_duplicate = version.content_sha256 in seen_hashes
        seen_hashes.add(version.content_sha256)
        factors = {
            "topic_relevance": min(len(matched) / max(len(keywords), 1), 1.0),
            "country_match": 1.0
            if str((research_case.scope or {}).get("country_iso3") or "").lower() in text
            else 0.5,
            "time_coverage": 1.0 if version.published_at else 0.4,
            "source_type": 1.0 if source.authority_level in {"official", "high"} else 0.6,
            "originality": 1.0
            if source.source_type in {"government", "official", "international_organization"}
            else 0.6,
            "deduplication": 0.0 if is_duplicate else 1.0,
            "existing_evidence": 1.0 if link.usage_type in {"support", "refute"} else 0.6,
        }
        score = round(sum(factors[key] * weights[key] for key in default_weights) * 100, 2)
        ranked.append(
            {
                "document_version_id": version.id,
                "title": version.title or document.title,
                "source": source.name,
                "source_type": source.source_type,
                "published_at": _iso(version.published_at),
                "source_url": document.canonical_url or document.discovery_url,
                "score": score,
                "factor_scores": factors,
                "matched_keywords": matched,
                "included": not is_duplicate,
                "exclusion_reason": "内容哈希重复，保留查看但不纳入优先队列" if is_duplicate else None,
            }
        )
    ranked.sort(key=lambda item: (-item["score"], item["document_version_id"]))
    return {
        "type": "material_relevance_ranking",
        "result_status": "candidate",
        "research_case_id": case_id,
        "query": query,
        "weights": weights,
        "ranked_materials": ranked,
        "included_materials": [item for item in ranked if item["included"]],
        "excluded_materials": [item for item in ranked if not item["included"]],
        "evidence_gaps": [] if ranked else ["专题内没有可排序的已确认材料。"],
        "suggested_next_skill": "S08",
        "processing_basis": list(default_weights),
        "method_note": "排序是可重现的候选优先级，低排名材料不丢失，不代表学术质量裁决。",
    }


def contradictory_evidence_context(
    session: Session, research_case_id: int | None, config: dict[str, Any]
) -> dict[str, Any]:
    case_id = int(research_case_id or config.get("research_case_id") or 0)
    if not case_id:
        raise ValueError("contradictory-evidence-context requires research_case_id")
    rows = _topic_documents(
        session,
        case_id,
        {int(item) for item in config.get("document_version_ids") or []},
    )
    cards = []
    for link, version, document, source in rows:
        cards.append(
            {
                "document_version_id": version.id,
                "title": version.title or document.title,
                "original_excerpt": version.abstract or "",
                "time": _iso(version.published_at) or version.issue_date_text,
                "place": source.country_or_region or None,
                "source": source.name,
                "source_type": source.source_type,
                "observer": source.organization_name or source.name,
                "position": link.usage_type,
                "source_url": document.canonical_url or document.discovery_url,
                "locator": {"document_version_id": version.id, "field": "abstract"},
            }
        )
    positions = {item["position"] for item in cards}
    candidates = []
    if "support" in positions and "refute" in positions:
        candidates.append(
            {
                "difference_type": "立场差异",
                "basis": "专题用途同时包含支持与反驳",
                "interpretation": "可能存在观察主体、概念或时点差异，需研究者判断。",
            }
        )
    if len({item["time"] for item in cards if item["time"]}) > 1:
        candidates.append(
            {
                "difference_type": "时间变化",
                "basis": "材料发布或表述时间不同",
                "interpretation": "差异可能来自情势变化，不直接判定任一条为真或假。",
            }
        )
    return {
        "type": "contradictory_evidence_context",
        "result_status": "candidate",
        "research_case_id": case_id,
        "context_cards": cards,
        "difference_candidates": candidates,
        "truth_judgment": None,
        "evidence_gaps": [] if len(cards) >= 2 else ["当前不足两份可对照材料。"],
        "suggested_next_skill": "S09",
        "processing_basis": ["专题用途", "时间", "来源类型", "观察主体"],
        "method_note": "所有来源并列展示，系统不按来源数量裁决真假。",
    }


def country_comparison(session: Session, config: dict[str, Any]) -> dict[str, Any]:
    countries = [str(item).upper() for item in config.get("country_iso3s") or []]
    if len(countries) < 2:
        raise ValueError("country-comparison requires at least two countries")
    codes = [str(item) for item in config.get("indicator_codes") or []]
    statement = (
        select(StructuredObservation, StructuredObservationVersion, StructuredDataset)
        .join(
            StructuredObservationVersion,
            (StructuredObservationVersion.observation_id == StructuredObservation.id)
            & (StructuredObservationVersion.version_no == StructuredObservation.latest_version_no),
        )
        .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
        .where(StructuredObservation.country_iso3.in_(countries))
    )
    if codes:
        statement = statement.where(StructuredObservation.indicator_code.in_(codes))
    period_from = str(config.get("period_from") or "")
    period_to = str(config.get("period_to") or "")
    if period_from:
        statement = statement.where(StructuredObservation.period >= period_from)
    if period_to:
        statement = statement.where(StructuredObservation.period <= period_to)
    rows = list(session.execute(statement))
    locators = {}
    if config.get("research_case_id"):
        rows, locators = adopted_observations(session, int(config["research_case_id"]), countries)
        rows = [
            row
            for row in rows
            if (not codes or row[0].indicator_code in codes)
            and (not period_from or row[0].period >= int(period_from))
            and (not period_to or row[0].period <= int(period_to))
        ]
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    invalid_rows = []
    for observation, version, dataset in rows:
        if not observation.indicator_code:
            invalid_rows.append(
                {"indicator_code": None, "period": observation.period, "reason": "指标代码缺失"}
            )
            continue
        key = (observation.indicator_code, observation.period, version.unit, dataset.dataset_key)
        if locators:
            key += (
                locators[version.id]["snapshot_id"],
                observation.partner_iso3,
                observation.metric_code,
                observation.commodity_classification,
                observation.commodity_code,
                observation.trade_flow,
                observation.partner2_code,
                observation.customs_code,
                observation.mot_code,
                observation.frequency,
                version.currency,
                version.price_basis,
            )
        item = groups.setdefault(
            key,
            {
                "indicator_code": observation.indicator_code,
                "period": observation.period,
                "unit": version.unit,
                "dataset_key": dataset.dataset_key,
                "values": {},
                "source_urls": {},
                "locators": {},
                "value_texts": {},
            },
        )
        item["values"][observation.country_iso3] = float(version.value) if version.value is not None else None
        item["source_urls"][observation.country_iso3] = version.source_url
        item["locators"][observation.country_iso3] = locators.get(version.id)
        item["value_texts"][observation.country_iso3] = (
            str(version.value) if version.value is not None else None
        )
    comparable = []
    not_comparable = list(invalid_rows)
    for item in groups.values():
        missing_countries = [country for country in countries if country not in item["values"]]
        null_countries = [country for country in countries if item["values"].get(country) is None]
        if missing_countries or null_countries or not item["unit"] or not item["dataset_key"]:
            reasons = []
            if missing_countries:
                reasons.append(f"缺少国家：{'、'.join(missing_countries)}")
            if null_countries:
                reasons.append(f"空值国家：{'、'.join(null_countries)}")
            if not item["unit"]:
                reasons.append("单位缺失")
            if not item["dataset_key"]:
                reasons.append("数据集标识缺失")
            not_comparable.append(
                {
                    "indicator_code": item["indicator_code"],
                    "period": item["period"],
                    "reason": "；".join(reasons),
                }
            )
            continue
        values = [item["values"][country] for country in countries]
        minimum = min(values)
        maximum = max(values)
        item["statistics"] = {
            "minimum": minimum,
            "maximum": maximum,
            "absolute_difference": maximum - minimum,
            "relative_difference": ((maximum - minimum) / abs(minimum)) if minimum not in {0} else None,
        }
        comparable.append(item)
    case_id = int(config.get("research_case_id") or 0)
    material_rows = _topic_documents(session, case_id) if case_id else []
    dimensions = [str(item).strip() for item in config.get("comparison_dimensions") or []]
    material_comparison = []
    for link, version, document, source in material_rows:
        material_comparison.append(
            {
                "document_version_id": version.id,
                "title": version.title or document.title,
                "country_or_region": source.country_or_region or None,
                "source": source.name,
                "published_at": _iso(version.published_at),
                "usage_type": link.usage_type,
                "dimensions": dimensions,
                "excerpt": version.abstract or "",
                "source_url": document.canonical_url or document.discovery_url,
            }
        )
    return {
        "type": "country_comparison",
        "result_status": "candidate",
        "countries": countries,
        "comparable_rows": comparable,
        "not_comparable": not_comparable,
        "material_comparison": material_comparison,
        "comparison_dimensions": dimensions,
        "evidence_gaps": [
            *([] if comparable else ["没有所有国家均为非空值且口径完全一致的指标。"]),
            *(["尚未指定材料比较维度。"] if material_rows and not dimensions else []),
        ],
        "suggested_next_skill": "S10",
        "processing_basis": ["指标代码", "时期", "单位", "数据集", "研究者指定的材料比较维度"],
        "method_note": "数值只比较口径完全对齐的观测；材料只按研究者指定维度并列，不机械排名或推断差异原因。",
    }


def policy_impact(session: Session, research_case_id: int | None, config: dict[str, Any]) -> dict[str, Any]:
    events = _event_scope(session, research_case_id, config, policy_only=True)
    event_ids = [item.id for item in events]
    claims = (
        list(
            session.execute(
                select(EvidenceClaim, EventMention, DocumentVersion, Document, Source)
                .join(EventMention, EventMention.id == EvidenceClaim.event_mention_id)
                .join(DocumentVersion, DocumentVersion.id == EventMention.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    EventMention.event_id.in_(event_ids),
                    EventMention.review_status == "confirmed",
                    EvidenceClaim.review_status == "confirmed",
                )
            )
        )
        if event_ids
        else []
    )
    case_id = research_case_id or config.get("research_case_id")
    if case_id:
        allowed = project_workflow.adopted_mention_ids(session, int(case_id))
        claims = [row for row in claims if row[1].id in allowed]
    nodes = []
    edges = []
    for event in events:
        nodes.append({"id": f"event:{event.id}", "type": "policy", "label": event.title})
    for claim, mention, version, document, source in claims:
        claim_id = f"claim:{claim.id}"
        nodes.append(
            {
                "id": claim_id,
                "type": "claim",
                "label": f"{claim.subject_text}{claim.predicate}{claim.value_text}",
            }
        )
        edges.append(
            {
                "id": f"claim-edge:{claim.id}",
                "from": f"event:{mention.event_id}",
                "to": claim_id,
                "relation": claim.predicate,
                "status": "claimed",
                "time": _iso(version.published_at),
                "generation_rule": "confirmed 事件提及中的 confirmed 主张",
                "evidence": {
                    "document_version_id": version.id,
                    "source": source.name,
                    "source_url": document.canonical_url or document.discovery_url,
                    "locator": claim.evidence_locator,
                },
            }
        )
    relations = (
        list(
            session.scalars(
                select(EventRelation).where(
                    EventRelation.source_event_id.in_(event_ids), EventRelation.target_event_id.in_(event_ids)
                )
            )
        )
        if event_ids
        else []
    )
    for relation in relations:
        edges.append(
            {
                "id": f"relation-edge:{relation.id}",
                "from": f"event:{relation.source_event_id}",
                "to": f"event:{relation.target_event_id}",
                "relation": relation.relation_type,
                "status": "observed",
                "time": None,
                "generation_rule": "已登记的事件关系",
                "evidence": {"note": relation.note},
            }
        )
    gaps = []
    if events and not claims:
        gaps.append("当前政策事件没有 confirmed 主张，不构造执行机制或影响结果。")
    if not events:
        gaps.append("专题中没有 reviewed 政策事件。")
    return {
        "type": "policy_impact",
        "result_status": "candidate",
        "policy_events": [
            {"event_id": item.id, "title": item.title, "summary": item.summary} for item in events
        ],
        "nodes": nodes,
        "edges": edges,
        "evidence_gaps": gaps,
        "edge_review_states": {item["id"]: "pending" for item in edges},
        "processing_basis": ["reviewed 政策事件", "confirmed 事件提及与主张", "已登记事件关系"],
        "method_note": (
            "边只标为已观察事实或来源主张；时间先后不自动表述为因果，未被证据支持的机制仅进入缺口。"
        ),
    }


def topic_feasibility(
    session: Session, research_case_id: int | None, config: dict[str, Any]
) -> dict[str, Any]:
    case_id = int(research_case_id or config.get("research_case_id") or 0)
    research_case = session.get(ResearchCase, case_id) if case_id else None
    question = str(
        config.get("research_question") or (research_case.research_question if research_case else "")
    ).strip()
    if not question:
        raise ValueError("topic-feasibility requires research_question")
    iso3 = str(
        config.get("country_iso3")
        or ((research_case.scope or {}).get("country_iso3") if research_case else "")
        or ""
    ).upper()
    document_rows = (
        list(
            session.execute(
                select(DocumentVersion, Document, Source)
                .join(ResearchCaseDocument, ResearchCaseDocument.document_version_id == DocumentVersion.id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(
                    ResearchCaseDocument.research_case_id == case_id,
                    _confirmed_document_version_clause(),
                )
            )
        )
        if case_id
        else []
    )
    events = _event_scope(session, case_id or None, {**config, "country_iso3": iso3})
    observations = (
        list(
            session.execute(
                select(StructuredObservation, StructuredObservationVersion, StructuredDataset)
                .join(
                    StructuredObservationVersion,
                    (StructuredObservationVersion.observation_id == StructuredObservation.id)
                    & (StructuredObservationVersion.version_no == StructuredObservation.latest_version_no),
                )
                .join(StructuredDataset, StructuredDataset.id == StructuredObservation.dataset_id)
                .where(
                    StructuredObservation.country_iso3 == iso3,
                    StructuredObservationVersion.value.is_not(None),
                )
            )
        )
        if iso3
        else []
    )
    locators = {}
    if case_id:
        document_rows = [
            (version, document, source) for _, version, document, source in _topic_documents(session, case_id)
        ]
        observations, locators = adopted_observations(session, case_id, [iso3])
        observations = [row for row in observations if row[1].value is not None]
    sources = {source.id for _, _, source in document_rows}
    periods = sorted({observation.period for observation, _, _ in observations if observation.period})
    dimensions = [
        {
            "key": "literature_basis",
            "label": "文献基础",
            "value": len(document_rows),
            "status": "ready" if len(document_rows) >= 3 else "limited",
        },
        {
            "key": "source_diversity",
            "label": "来源多样性",
            "value": len(sources),
            "status": "ready" if len(sources) >= 2 else "limited",
        },
        {
            "key": "data_availability",
            "label": "数据可得性",
            "value": len(observations),
            "status": "ready" if observations else "limited",
        },
        {
            "key": "time_coverage",
            "label": "时间覆盖",
            "value": {"from": periods[0] if periods else None, "to": periods[-1] if periods else None},
            "status": "ready" if len(periods) >= 3 else "limited",
        },
        {
            "key": "method_fit",
            "label": "方法匹配",
            "value": [
                "事件过程追踪" if events else "补充事件证据",
                "结构化指标比较" if observations else "补充可比数据",
            ],
            "status": "expert_review_required",
        },
        {
            "key": "practical_relevance",
            "label": "现实意义",
            "value": len(events),
            "status": "candidate" if events else "limited",
        },
    ]
    title = research_case.title if research_case else question.rstrip("？?")[:40]
    return {
        "type": "topic_feasibility",
        "observation_locators": list(locators.values()),
        "country_iso3": iso3 or None,
        "research_question": question,
        "candidate_titles": [title, f"{iso3 or '目标国'}：{question.rstrip('？?')[:32]}"],
        "dimensions": dimensions,
        "evidence_summary": {
            "documents": len(document_rows),
            "sources": len(sources),
            "events": len(events),
            "structured_observations": len(observations),
        },
        "method_suggestions": ["事件过程追踪", "多源证据对比", "结构化指标比较"],
        "data_needs": [item["label"] for item in dimensions if item["status"] == "limited"],
        "confirmation_questions": [
            "是否确定国家与时间范围？",
            "是否由领域专家复核方法匹配？",
            "是否进入现有专题确认建题流程？",
        ],
        "created_research_case": False,
        "evidence_gaps": [f"需补充：{item['label']}" for item in dimensions if item["status"] == "limited"],
        "suggested_next_skill": "S01",
        "method_note": "分项是确定性证据覆盖提示，不自动创建专题；方法匹配必须由领域专家复核。",
    }


def material_condition_assessment(
    session: Session, research_case_id: int | None, config: dict[str, Any]
) -> dict[str, Any]:
    """评估现有研究资料条件，不判定选题是否可行。"""
    legacy = topic_feasibility(session, research_case_id, config)
    dimensions = []
    for item in legacy["dimensions"]:
        dimensions.append(
            {
                **item,
                "status": "expert_review_required"
                if item["key"] in {"method_fit", "practical_relevance"}
                else item["status"],
            }
        )
    research_case = session.get(ResearchCase, int(research_case_id or config.get("research_case_id") or 0))
    scope = research_case.scope if research_case else {}
    access_limits = [
        "只读取专题内已授权材料与可引用结构化数据",
        "受限田野资料需按专题成员权限人工确认",
    ]
    language = str(scope.get("language") or config.get("language") or "未指定")
    return {
        "type": "material_condition_assessment",
        "result_status": "candidate",
        "country_iso3": legacy["country_iso3"],
        "research_question": legacy["research_question"],
        "dimensions": dimensions,
        "evidence_summary": legacy["evidence_summary"],
        "literature_context": {
            "document_count": legacy["evidence_summary"]["documents"],
            "source_count": legacy["evidence_summary"]["sources"],
        },
        "material_coverage": {
            "events": legacy["evidence_summary"]["events"],
            "structured_observations": legacy["evidence_summary"]["structured_observations"],
        },
        "language_conditions": {"primary_language": language, "expert_review_required": True},
        "access_limits": access_limits,
        "data_needs": legacy["data_needs"],
        "expert_questions": [
            "当前材料是否覆盖研究问题的关键时期？",
            "来源结构是否存在机构、语言或地域偏差？",
            "研究方法与现有证据类型是否匹配？",
        ],
        "feasibility_conclusion": None,
        "created_research_case": False,
        "evidence_gaps": legacy["evidence_gaps"],
        "suggested_next_skill": "S01",
        "processing_basis": ["专题材料", "来源多样性", "时间覆盖", "结构化数据", "语言与访问限制"],
        "method_note": "只呈现资料条件和待专家判断问题，不输出可行/不可行结论，不自动创建选题。",
    }
