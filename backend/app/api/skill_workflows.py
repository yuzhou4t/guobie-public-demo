"""Lightweight researcher interfaces for the S03 and S05 workflows."""

from contextlib import ExitStack
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.research import (
    DbSession,
    FieldMaterialCreatePayload,
    ReaderActor,
    _capability_config_visible,
    _case_permission,
    _create_capability_config_revision,
    _field_material_summary,
    _require_capability_run_access,
    _run_summary,
    require_internal_reader_access,
    require_reader_write_access,
)
from app.models import (
    CapabilityConfig,
    CapabilityRun,
    CapabilityRunRevision,
    CapabilityRunTarget,
    CapabilityTemplate,
    FieldMaterial,
    ResearchCase,
    Source,
)
from app.models.skill_workflows import (
    FieldAnnotation,
    FieldExcerpt,
    FieldMaterialProfile,
    FieldSegment,
    FieldTextVersion,
    TrackingItem,
    TrackingReview,
)
from app.models.tracking_schedule import TrackingObservation
from app.services import field_access, field_ai_access, field_workflow, tracking_workflow, workflow_runtime
from app.services.field_materials import FieldMaterialValidationError, store_field_material
from app.services.reader_access import record_contribution

router = APIRouter(
    prefix="/api/v1/reader/skill-workflows",
    tags=["skill-workflows"],
    dependencies=[Depends(require_internal_reader_access)],
)
WRITE = [Depends(require_reader_write_access)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadingSegment(Input):
    locator: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=150)
    speaker: str = Field(default="", max_length=100)
    kind: Literal["transcript", "supplement"] = "transcript"


class ReadingNote(Input):
    locator: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=2000)


class Context(Input):
    reading_notes: list[ReadingNote] = Field(default_factory=list, max_length=2000)
    reading_segments: list[ReadingSegment] = Field(default_factory=list, max_length=2000)
    reading_note: str = Field(default="", max_length=1000)
    research_topic: str = Field(default="", max_length=200)
    participant_alias: str = Field(default="", max_length=100)
    researcher_reflection: str = Field(default="", max_length=2000)
    regions: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="zh", max_length=30)
    speaker_role: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=200)
    collection_method: str = Field(default="已有转写文本", max_length=100)
    usage_scope: str = Field(default="本项目内部研究", max_length=200)
    sample_note: str = Field(default="", max_length=500)
    source_name: str = Field(default="", max_length=200)
    source_url: str = Field(default="", max_length=2000)
    published_at: str | None = Field(default=None, max_length=40)
    article: str | None = Field(default=None, pattern=r"^\d{1,4}$")


class MaterialInput(FieldMaterialCreatePayload):
    context: Context = Field(default_factory=Context)
    sensitivity: Literal["ordinary", "unknown", "sensitive"] = "unknown"


class SourceInput(Input):
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(default="", max_length=300)
    source_name: str = Field(default="", max_length=200)
    published_at: str | None = Field(default=None, max_length=40)
    language: str = Field(default="fr", max_length=30)
    compare_content: bool = False
    article: str | None = Field(default=None, pattern=r"^\d{1,4}$")


class WorkflowConfig(Input):
    workflow_version: int = 1
    material_ids: list[int] = Field(default_factory=list, max_length=15)
    instruction: str = Field(
        default="保留原意，只整理明确口语噪声；对有研究价值的完整片段提供候选标注。", max_length=2000
    )
    country_iso3: str = Field(default="", max_length=3)
    regions: list[str] = Field(default_factory=list, max_length=20)
    topic_name: str = Field(default="", max_length=160)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    focus_types: list[str] = Field(default_factory=list, max_length=10)
    source_ids: list[int] = Field(default_factory=list, max_length=3)
    sources: list[SourceInput] = Field(default_factory=list, max_length=8)
    date_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ConfigInput(Input):
    kind: Literal["s03", "s05"]
    research_case_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=160)
    config: WorkflowConfig
    config_id: int | None = None


def material_access(db, material_id, actor, permission="view", project_id=None):
    row = db.get(FieldMaterial, material_id)
    if not row:
        raise HTTPException(404, "材料不存在")
    if project_id is None and actor.id != row.owner_id:
        project_id = row.research_case_id
    try:
        field_access.resolve_field_material_access(
            db, actor, material_id, project_id, permission, require_link=project_id is not None
        )
    except field_access.FieldAccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    return row


def contribution(db, actor, case_id, action, key):
    if case_id is None:
        field_access.audit(db, actor.id, None, action, object_key=key)
        return
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type=action,
        object_type="skill_workflow",
        object_key=key,
        details={},
    )


@router.get("/context")
def context(db: DbSession, actor: ReaderActor):
    cases = []
    for case in db.scalars(select(ResearchCase).where(ResearchCase.status != "archived")):
        try:
            _case_permission(db, case.id, actor, "view")
            cases.append({"id": case.id, "title": case.title})
        except HTTPException:
            continue
    configs = [
        dict(
            id=c.id,
            name=c.name,
            research_case_id=c.research_case_id,
            config=c.config,
            kind="s05" if db.get(CapabilityTemplate, c.template_id).catalog_key == "research-s05" else "s03",
            status=c.status,
        )
        for c in db.scalars(select(CapabilityConfig).order_by(CapabilityConfig.id))
        if c.config.get("workflow_version") == 1 and _capability_config_visible(db, c, actor)
    ]
    return {
        "cases": cases,
        "configs": configs,
        "sources": [
            {"id": s.id, "name": s.name}
            for s in db.scalars(select(Source).where(Source.status == "active").order_by(Source.name))
        ],
    }


@router.post("/configs", dependencies=WRITE)
def save_config(payload: ConfigInput, db: DbSession, actor: ReaderActor):
    _case_permission(db, payload.research_case_id, actor, "edit")
    template = db.scalar(
        select(CapabilityTemplate).where(CapabilityTemplate.catalog_key == f"research-{payload.kind}")
    )
    if not template:
        raise HTTPException(404, "能力模板尚未登记")
    config = payload.config.model_dump()
    config["workflow_version"] = 1
    for material_id in config["material_ids"]:
        material_access(
            db, material_id, actor, "ai" if payload.kind == "s05" else "read", payload.research_case_id
        )
    if payload.kind == "s03":
        if (
            not config["topic_name"].strip()
            or not any(x.strip() for x in config["keywords"])
            or not (config["country_iso3"] or config["regions"])
        ):
            raise HTTPException(422, "请填写国家或区域、主题和关键词")
        if not any(config[k] for k in ("material_ids", "source_ids", "sources")):
            raise HTTPException(422, "请选择至少一种来源")
        if config["date_from"] and config["date_to"] and config["date_from"] > config["date_to"]:
            raise HTTPException(422, "起始时间不能晚于截止时间")
    row = db.get(CapabilityConfig, payload.config_id) if payload.config_id else None
    if payload.config_id and (
        not row or row.research_case_id != payload.research_case_id or row.template_id != template.id
    ):
        raise HTTPException(404, "未找到当前项目的能力配置")
    if not row:
        row = db.scalar(
            select(CapabilityConfig).where(
                CapabilityConfig.research_case_id == payload.research_case_id,
                CapabilityConfig.name == payload.name,
            )
        )
        if row:
            raise HTTPException(409, "同名配置已存在，请选择已有配置")
        row = CapabilityConfig(
            template_id=template.id,
            owner_id=actor.id,
            scope_type="research_case",
            research_case_id=payload.research_case_id,
            name=payload.name,
            config={},
        )
        db.add(row)
    row.name, row.config = payload.name, config
    row.status = "active" if template.validation_status == "verified" else "draft"
    db.flush()
    _create_capability_config_revision(db, row, template, actor.id)
    contribution(db, actor, payload.research_case_id, "workflow_config_saved", row.id)
    db.commit()
    return {"id": row.id, "status": row.status, "can_run": row.status == "active"}


@router.post("/materials", dependencies=WRITE)
def upload_material(payload: MaterialInput, db: DbSession, actor: ReaderActor):
    from app.api.research import _require_catalog_country

    if payload.research_case_id:
        _case_permission(db, payload.research_case_id, actor, "edit")
        case = db.get(ResearchCase, payload.research_case_id)
        country = (case.scope or {}).get("country_iso3")
        if payload.country_iso3 and country and country != payload.country_iso3:
            raise HTTPException(422, "资料国家与项目不一致")
    else:
        country = payload.country_iso3
    if payload.country_iso3 or country:
        _require_catalog_country(payload.country_iso3 or country)
    try:
        stored = store_field_material(
            filename=payload.filename,
            reported_content_type=payload.content_type,
            content_base64=payload.content_base64,
        )
        units = field_workflow.extract_units(stored.path.read_bytes(), stored.content_type)
    except (FieldMaterialValidationError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    row = FieldMaterial(
        owner_id=actor.id,
        research_case_id=payload.research_case_id,
        country_iso3=payload.country_iso3 or country,
        title=payload.title,
        original_filename=payload.filename,
        storage_key=stored.storage_key,
        content_type=stored.content_type,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        material_type=payload.material_type,
        privacy_level=payload.privacy_level,
        evidence_status="user_provided_unreviewed",
        authorization_confirmed=True,
        captured_on=payload.captured_on,
        method_note=payload.method_note,
    )
    db.add(row)
    db.flush()
    field_access.ensure_asset(db, row)
    if payload.research_case_id:
        field_access.owner_project_link(db, row, actor.id, payload.research_case_id)
    db.add(
        FieldMaterialProfile(
            material_id=row.id, context=payload.context.model_dump(), sensitivity=payload.sensitivity
        )
    )
    field_workflow.add_version(db, row, units, "raw", actor.id, note="原样导入；原始文件字节另存")
    contribution(db, actor, row.research_case_id, "workflow_material_uploaded", row.id)
    db.commit()
    return _field_material_summary(row)


@router.get("/cases/{case_id}/materials")
def materials(case_id: int, db: DbSession, actor: ReaderActor):
    _case_permission(db, case_id, actor, "view")
    result = []
    for row in db.scalars(
        select(FieldMaterial)
        .where(FieldMaterial.id.in_(field_access.project_material_ids(db, case_id)))
        .order_by(FieldMaterial.id)
    ):
        try:
            material_access(db, row.id, actor, project_id=case_id)
        except HTTPException:
            continue
        profile = db.get(FieldMaterialProfile, row.id)
        result.append(
            {
                **_field_material_summary(row),
                "project_id": case_id,
                "context": profile.context if profile else {},
                "sensitivity": profile.sensitivity if profile else "unknown",
            }
        )
    return {"items": result}


@router.get("/materials/{material_id}")
def read_material(material_id: int, db: DbSession, actor: ReaderActor, research_case_id: int | None = None):
    row = material_access(db, material_id, actor, project_id=research_case_id)
    profile = db.get(FieldMaterialProfile, row.id)
    versions = list(
        db.scalars(
            select(FieldTextVersion)
            .where(FieldTextVersion.material_id == material_id)
            .order_by(FieldTextVersion.version_no)
        )
    )
    visible = []
    for version in versions:
        if version.kind == "raw":
            visible.append(version)
        elif version.run_id:
            source_run = db.get(CapabilityRun, version.run_id)
            if source_run and (
                source_run.research_case_id == research_case_id
                or (research_case_id is None and row.owner_id == actor.id)
            ):
                try:
                    from app.services.field_access import FieldAccessDenied, enforce_run_access

                    enforce_run_access(db, source_run, actor)
                    visible.append(version)
                except FieldAccessDenied:
                    pass
        elif row.owner_id == actor.id:
            visible.append(version)
    versions = visible
    return {
        **_field_material_summary(row),
        "context": profile.context if profile else {},
        "sensitivity": profile.sensitivity if profile else "unknown",
        "versions": [
            {
                "id": v.id,
                "kind": v.kind,
                "version_no": v.version_no,
                "parent_id": v.parent_id,
                "units": v.units,
                "note": v.note,
                "created_at": v.created_at,
                "created_by": v.created_by,
            }
            for v in versions
        ],
    }


class MetadataInput(Input):
    base_context: dict | None = None
    context: Context
    sensitivity: Literal["ordinary", "unknown", "sensitive"]
    privacy_level: Literal["restricted", "anonymized", "shareable"]
    captured_on: date | None = None


@router.post("/materials/{material_id}/metadata", dependencies=WRITE)
def revise_metadata(material_id: int, payload: MetadataInput, db: DbSession, actor: ReaderActor):
    material = material_access(db, material_id, actor, "confirm")
    if material.owner_id != actor.id:
        raise HTTPException(403, "原资料语境与公开范围由上传者维护")
    db.scalar(select(FieldMaterial).where(FieldMaterial.id == material_id).with_for_update())
    profile = db.get(FieldMaterialProfile, material_id)
    if payload.base_context is not None and payload.base_context != (profile.context if profile else {}):
        raise HTTPException(409, "研究者笔记或材料语境已有更新，请保留输入并刷新")
    raw = field_workflow.latest_version(db, material_id, "raw")
    selected = [item.locator for item in payload.context.reading_segments]
    known = {unit["locator"] for unit in raw.units} if raw else set()
    if len(selected) != len(set(selected)) or not set(selected) <= known:
        raise HTTPException(422, "阅读段落必须对应不重复的原文位置")
    noted = [item.locator for item in payload.context.reading_notes]
    if len(noted) != len(set(noted)) or not set(noted) <= known:
        raise HTTPException(422, "研究批注必须对应不重复的原文位置")
    if not profile:
        profile = FieldMaterialProfile(material_id=material_id)
        db.add(profile)
    before = {
        "context": profile.context,
        "sensitivity": profile.sensitivity,
        "privacy_level": material.privacy_level,
        "captured_on": str(material.captured_on or ""),
    }
    profile.context = payload.context.model_dump()
    profile.sensitivity = payload.sensitivity
    material.privacy_level = payload.privacy_level
    material.captured_on = payload.captured_on
    field_access.audit(
        db,
        actor.id,
        material_id,
        "workflow_metadata_revised",
        before=before,
        after=payload.model_dump(mode="json"),
    )
    db.commit()
    return {"id": material_id}


class CleanInput(Input):
    source_version_id: int | None = None
    base_version_id: int
    units: list[workflow_runtime.CleanUnit] = Field(min_length=1, max_length=2000)
    note: str = Field(default="研究者校对", max_length=500)


@router.post("/materials/{material_id}/versions", dependencies=WRITE)
def revise_text(material_id: int, payload: CleanInput, db: DbSession, actor: ReaderActor):
    material = material_access(db, material_id, actor, "revise")
    db.scalar(select(FieldMaterial).where(FieldMaterial.id == material_id).with_for_update())
    latest = field_workflow.latest_version(db, material_id)
    if not latest or latest.id != payload.base_version_id:
        raise HTTPException(409, "整理稿已有新版本，请刷新后再编辑")
    raw = field_workflow.latest_version(db, material_id, "raw")
    if [x.locator for x in payload.units] != [x["locator"] for x in raw.units]:
        raise HTTPException(422, "人工修订必须保留全部原文位置")
    source = db.get(FieldTextVersion, payload.source_version_id) if payload.source_version_id else latest
    if not source or source.material_id != material_id:
        raise HTTPException(422, "修订来源版本不属于当前材料")
    row = field_workflow.add_version(
        db,
        material,
        [x.model_dump() for x in payload.units],
        "clean",
        actor.id,
        parent=source,
        note=payload.note,
    )
    contribution(db, actor, material.research_case_id, "workflow_text_revised", row.id)
    db.commit()
    return {"id": row.id, "version_no": row.version_no}


def annotation_payload(db, segment):
    versions = list(
        db.scalars(
            select(FieldAnnotation)
            .where(FieldAnnotation.segment_id == segment.id)
            .order_by(FieldAnnotation.revision_no)
        )
    )
    latest = versions[-1]
    return {
        "id": segment.id,
        "material_id": segment.material_id,
        "run_id": segment.run_id,
        "raw_version_id": segment.raw_version_id,
        "clean_version_id": segment.clean_version_id,
        "positions": segment.positions,
        "original_text": segment.original_text,
        "annotation": {
            "id": latest.id,
            "revision_no": latest.revision_no,
            "status": latest.status,
            **latest.payload,
        },
        "history": [
            {
                "id": v.id,
                "status": v.status,
                "payload": v.payload,
                "revision_no": v.revision_no,
                "created_at": v.created_at,
            }
            for v in versions
        ],
    }


@router.get("/runs/{run_id}")
def read_workflow_run(run_id: int, db: DbSession, actor: ReaderActor):
    run = db.get(CapabilityRun, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    _require_capability_run_access(db, run, actor)
    segments = []
    for segment in db.scalars(select(FieldSegment).where(FieldSegment.run_id == run_id)):
        material_access(db, segment.material_id, actor, project_id=run.research_case_id)
        segments.append(annotation_payload(db, segment))
    excerpts = []
    for item, annotation, segment in db.execute(
        select(FieldExcerpt, FieldAnnotation, FieldSegment)
        .join(FieldAnnotation, FieldExcerpt.annotation_id == FieldAnnotation.id)
        .join(FieldSegment, FieldAnnotation.segment_id == FieldSegment.id)
        .where(FieldSegment.run_id == run_id)
    ):
        excerpts.append(
            {
                "id": item.id,
                "is_current": db.scalar(
                    select(FieldAnnotation.id)
                    .where(FieldAnnotation.segment_id == segment.id)
                    .order_by(FieldAnnotation.revision_no.desc())
                    .limit(1)
                )
                == annotation.id,
                "kind": item.kind,
                "segment_id": segment.id,
                "annotation_id": annotation.id,
                "material_id": segment.material_id,
                "text": segment.original_text,
                "annotation": annotation.payload,
                "positions": segment.positions,
                "raw_version_id": segment.raw_version_id,
                "clean_version_id": segment.clean_version_id,
            }
        )
    reviews = {}
    review_history = {}
    for obs in run.output.get("observations", []):
        item = db.get(TrackingItem, obs["item_id"])
        if item and item.material_id:
            material_access(db, item.material_id, actor, project_id=run.research_case_id)
        observation = (
            db.get(TrackingObservation, obs["observation_id"]) if obs.get("observation_id") else None
        )
        observation = observation or db.scalar(
            select(TrackingObservation).where(
                TrackingObservation.run_id == run.id, TrackingObservation.item_id == obs["item_id"]
            )
        )
        if observation:
            obs["observation_id"] = observation.id
        rows = list(
            db.scalars(
                select(TrackingReview)
                .where(
                    TrackingReview.item_id == obs["item_id"],
                    TrackingReview.observation_id == (observation.id if observation else -1),
                )
                .order_by(TrackingReview.revision_no)
            )
        )
        review_history[str(obs["item_id"])] = [
            {
                "revision_no": r.revision_no,
                "payload": r.payload,
                "created_at": r.created_at,
                "created_by": r.created_by,
            }
            for r in rows
        ]
        reviews[str(obs["item_id"])] = (
            {"revision_no": rows[-1].revision_no, **rows[-1].payload}
            if rows
            else {"revision_no": 0, "decision": "pending", "relation": "unreviewed"}
        )
    return {
        **_run_summary(run),
        "config_id": run.config_id,
        "research_case_id": run.research_case_id,
        "output": run.output,
        "error_message": run.error_message,
        "segments": segments,
        "excerpts": excerpts,
        "reviews": reviews,
        "review_history": review_history,
        "saved_versions": [
            {
                "id": v.id,
                "revision_no": v.revision_no,
                "artifact": v.artifact,
                "created_at": v.created_at,
                "created_by": v.created_by,
            }
            for v in db.scalars(
                select(CapabilityRunRevision)
                .where(CapabilityRunRevision.capability_run_id == run_id)
                .order_by(CapabilityRunRevision.revision_no)
            )
        ],
    }


class AnnotationInput(Input):
    base_revision_no: int
    status: Literal["candidate", "confirmed", "modified", "rejected"]
    themes: list[str] = Field(max_length=8)
    viewpoint: str = Field(max_length=3000)
    statement_type: str = Field(max_length=30)
    verification: Literal["unverified", "verified", "conflict", "not_applicable"]
    note: str = Field(default="", max_length=2000)
    sensitive: bool = False


@router.post("/segments/{segment_id}/annotations", dependencies=WRITE)
def revise_annotation(segment_id: int, payload: AnnotationInput, db: DbSession, actor: ReaderActor):
    segment = db.scalar(select(FieldSegment).where(FieldSegment.id == segment_id).with_for_update())
    if not segment:
        raise HTTPException(404, "片段不存在")
    run = db.get(CapabilityRun, segment.run_id)
    _require_capability_run_access(db, run, actor, "revise")
    material_access(db, segment.material_id, actor, "revise", run.research_case_id)
    if payload.status in {"confirmed", "modified"} or payload.verification == "verified":
        _case_permission(db, run.research_case_id, actor, "confirm")
    latest = db.scalar(
        select(FieldAnnotation)
        .where(FieldAnnotation.segment_id == segment_id)
        .order_by(FieldAnnotation.revision_no.desc())
        .limit(1)
    )
    if payload.base_revision_no != latest.revision_no:
        raise HTTPException(409, "标注已有新修订，请刷新")
    row = FieldAnnotation(
        segment_id=segment_id,
        revision_no=latest.revision_no + 1,
        status=payload.status,
        payload=payload.model_dump(exclude={"base_revision_no", "status"}),
        created_by=actor.id,
    )
    db.add(row)
    contribution(db, actor, run.research_case_id, "workflow_annotation_reviewed", segment_id)
    db.commit()
    return {"id": row.id, "revision_no": row.revision_no}


class ExcerptInput(Input):
    annotation_id: int
    kind: Literal["excerpt", "viewpoint"] = "excerpt"


@router.post("/excerpts", dependencies=WRITE)
def create_excerpt(payload: ExcerptInput, db: DbSession, actor: ReaderActor):
    annotation = db.get(FieldAnnotation, payload.annotation_id)
    if not annotation:
        raise HTTPException(404, "标注不存在")
    segment = db.get(FieldSegment, annotation.segment_id)
    run = db.get(CapabilityRun, segment.run_id)
    _require_capability_run_access(db, run, actor, "confirm")
    material_access(db, segment.material_id, actor, "cite", run.research_case_id)
    if annotation.status not in {"confirmed", "modified"}:
        raise HTTPException(422, "只有研究者已确认的标注才能生成摘录或观点卡")
    latest = db.scalar(
        select(FieldAnnotation)
        .where(FieldAnnotation.segment_id == segment.id)
        .order_by(FieldAnnotation.revision_no.desc())
        .limit(1)
    )
    if latest.id != annotation.id:
        raise HTTPException(409, "请使用最新已确认标注")
    row = db.scalar(
        select(FieldExcerpt).where(
            FieldExcerpt.annotation_id == annotation.id, FieldExcerpt.kind == payload.kind
        )
    )
    if not row:
        row = FieldExcerpt(annotation_id=annotation.id, kind=payload.kind, created_by=actor.id)
        db.add(row)
        db.flush()
        contribution(db, actor, run.research_case_id, "workflow_excerpt_saved", row.id)
    db.commit()
    return {"id": row.id}


class TrackingInput(Input):
    observation_id: int | None = None
    previous_observation_id: int | None = None
    base_revision_no: int
    decision: Literal["pending", "saved", "ignored", "to_verify"] = "pending"
    note: str = Field(default="", max_length=2000)
    relation: Literal["unreviewed", "new", "revision", "replacement", "duplicate", "unknown", "unlinked"] = (
        "unreviewed"
    )
    previous_item_id: int | None = None
    compare: bool = False
    edited_candidate: workflow_runtime.TrackingDraft | None = None
    candidate_status: Literal["candidate", "confirmed", "rejected"] = "candidate"


@router.post("/tracking-items/{item_id}/reviews", dependencies=WRITE)
def review_tracking(item_id: int, payload: TrackingInput, db: DbSession, actor: ReaderActor):
    item = db.scalar(select(TrackingItem).where(TrackingItem.id == item_id).with_for_update())
    if not item:
        raise HTTPException(404, "材料不存在")
    config = db.get(CapabilityConfig, item.config_id)
    _case_permission(
        db,
        config.research_case_id,
        actor,
        "confirm" if payload.decision == "saved" or payload.compare else "revise",
    )
    if item.material_id:
        material_access(db, item.material_id, actor, project_id=config.research_case_id)
    observation = db.get(TrackingObservation, payload.observation_id) if payload.observation_id else None
    if observation is None and payload.observation_id is None:
        known = list(db.scalars(select(TrackingObservation).where(TrackingObservation.item_id == item_id)))
        if len(known) == 1:
            observation = known[0]
    if not observation or observation.item_id != item_id:
        raise HTTPException(422, "请指定本轮不可变观察快照，历史审核不能自动用于新内容")
    source_run = db.get(CapabilityRun, observation.run_id)
    _require_capability_run_access(db, source_run, actor)
    latest = db.scalar(
        select(TrackingReview)
        .where(TrackingReview.item_id == item_id, TrackingReview.observation_id == observation.id)
        .order_by(TrackingReview.revision_no.desc())
        .limit(1)
    )
    if (latest.revision_no if latest else 0) != payload.base_revision_no:
        raise HTTPException(409, "该观察已有新处理记录，请刷新")
    global_latest = db.scalar(
        select(TrackingReview)
        .where(TrackingReview.item_id == item_id)
        .order_by(TrackingReview.revision_no.desc())
        .limit(1)
    )
    number = global_latest.revision_no if global_latest else 0
    result = payload.model_dump(exclude={"base_revision_no", "compare", "edited_candidate"})
    result["observation_id"] = observation.id
    if latest and "candidate_status" not in payload.model_fields_set:
        result["candidate_status"] = latest.payload.get("candidate_status", "candidate")
    if payload.relation in {"revision", "replacement", "duplicate"}:
        previous = db.get(TrackingItem, payload.previous_item_id) if payload.previous_item_id else None
        if not previous or previous.config_id != item.config_id or previous.id == item.id:
            raise HTTPException(422, "请指定同一任务中的另一份对应材料")
        if previous.material_id:
            material_access(db, previous.material_id, actor, project_id=config.research_case_id)
    else:
        result["previous_item_id"] = None

    if (
        latest
        and latest.payload.get("comparison")
        and payload.relation == latest.payload.get("relation")
        and payload.previous_item_id == latest.payload.get("previous_item_id")
    ):
        result["comparison"] = latest.payload["comparison"]
    if payload.compare:
        previous = db.get(TrackingItem, payload.previous_item_id)
        if not previous or previous.config_id != item.config_id or previous.id == item.id:
            raise HTTPException(422, "请选择同一任务内的另一份旧材料")
        if previous.material_id:
            material_access(db, previous.material_id, actor, project_id=config.research_case_id)
        if payload.relation not in {"revision", "replacement"}:
            raise HTTPException(422, "请先确认修订或替代关系")
        try:
            previous_observation = (
                db.get(TrackingObservation, payload.previous_observation_id)
                if payload.previous_observation_id
                else None
            )
            if not previous_observation:
                known = list(
                    db.scalars(select(TrackingObservation).where(TrackingObservation.item_id == previous.id))
                )
                previous_observation = known[0] if len(known) == 1 else None
            if not previous_observation or previous_observation.item_id != previous.id:
                raise ValueError("请指定用于比较的旧观察版本")
            _require_capability_run_access(db, db.get(CapabilityRun, previous_observation.run_id), actor)
            result["previous_observation_id"] = previous_observation.id
            before, after = previous_observation.snapshot, observation.snapshot
            changes = tracking_workflow.compare(before, after)
            with ExitStack() as stack:
                for source_item, source_observation in (
                    (previous, previous_observation),
                    (item, observation),
                ):
                    if source_item.material_id:
                        source_run = db.get(CapabilityRun, source_observation.run_id)
                        stack.enter_context(
                            field_ai_access.authorized_source(
                                db, source_run, source_item.material_id, actor_id=actor.id
                            )
                        )
                candidate = workflow_runtime.summarize_difference(before, after, changes)
            result["comparison"] = {
                "before": before,
                "after": after,
                "changes": changes,
                "candidate": candidate,
            }
        except (ValueError, workflow_runtime.AgentRuntimeError) as exc:
            result["comparison_error"] = str(exc)
            result.pop("comparison", None)
    if payload.edited_candidate is not None:
        if "comparison" not in result:
            raise HTTPException(422, "请先生成文本差异")
        units = {x["locator"]: x["text"] for x in result["comparison"]["after"]["units"]}
        if any(
            not x.original_text
            or x.original_text not in units.get(x.locator, "")
            or x.name not in x.original_text
            for x in payload.edited_candidate.entities
        ):
            raise HTTPException(422, "主体名称和引用必须保持原文依据；可以删除无效候选")
        result["comparison"] = {**result["comparison"], "candidate": payload.edited_candidate.model_dump()}
    if payload.candidate_status == "confirmed":
        _case_permission(db, config.research_case_id, actor, "confirm")
    row = TrackingReview(
        item_id=item_id,
        observation_id=observation.id,
        revision_no=number + 1,
        payload=result,
        created_by=actor.id,
    )
    db.add(row)
    contribution(db, actor, config.research_case_id, "workflow_tracking_reviewed", item_id)
    db.commit()
    return {"id": row.id, "revision_no": row.revision_no}


@router.post("/runs/{run_id}/save", dependencies=WRITE)
def save_results(run_id: int, db: DbSession, actor: ReaderActor):
    run = db.get(CapabilityRun, run_id)
    if not run:
        raise HTTPException(404, "运行不存在")
    _require_capability_run_access(db, run, actor, "confirm")
    view = read_workflow_run(run_id, db, actor)
    if run.status != "succeeded":
        raise HTTPException(422, "只能保存已完成运行")
    if run.output.get("type") == "field_research_workflow":
        artifact = {
            "type": "field_research_workflow",
            "excerpts": [x for x in view["excerpts"] if x["is_current"]],
            "source_run_id": run.id,
        }
        if not artifact["excerpts"]:
            raise HTTPException(422, "请先确认标注并生成摘录或观点卡")
    elif run.output.get("type") == "policy_tracking_workflow":
        artifact = {
            "type": "policy_tracking_workflow",
            "observations": [
                obs
                for obs in run.output["observations"]
                if view["reviews"][str(obs["item_id"])]["decision"] == "saved"
            ],
            "reviews": view["reviews"],
            "source_run_id": run.id,
        }
        if not artifact["observations"]:
            raise HTTPException(422, "请先选择保存的材料")
    else:
        raise HTTPException(422, "不是本工作流产物")
    from app.services.workflow_titles import workflow_result_title

    artifact["title"] = workflow_result_title(artifact, run.input_snapshot.get("config", {}))
    db.scalar(select(CapabilityRun).where(CapabilityRun.id == run_id).with_for_update())
    latest = db.scalar(
        select(CapabilityRunRevision)
        .where(CapabilityRunRevision.capability_run_id == run_id)
        .order_by(CapabilityRunRevision.revision_no.desc())
        .limit(1)
    )
    existing = db.scalar(select(CapabilityRunTarget).where(CapabilityRunTarget.capability_run_id == run_id))
    if latest and latest.artifact == artifact and existing:
        return {"id": existing.id, "revision_no": latest.revision_no, "idempotent": True}
    revision = CapabilityRunRevision(
        capability_run_id=run_id,
        revision_no=(latest.revision_no if latest else 0) + 1,
        artifact=artifact,
        note="研究者保存工作流结果",
        created_by=actor.id,
    )
    db.add(revision)
    db.flush()
    if existing:
        existing.artifact_snapshot = {
            **artifact,
            "revision_id": revision.id,
            "saved_revision_no": revision.revision_no,
        }
        existing.confirmed_at = datetime.now(UTC)
        existing.confirmed_by = actor.id
        contribution(db, actor, run.research_case_id, "workflow_results_saved", run_id)
        db.commit()
        return {"id": existing.id, "revision_no": revision.revision_no}
    row = CapabilityRunTarget(
        capability_run_id=run_id,
        target_type="research_case",
        target_key=str(run.research_case_id),
        confirmed_by=actor.id,
        confirmed_at=datetime.now(UTC),
        artifact_snapshot={**artifact, "revision_id": revision.id, "saved_revision_no": revision.revision_no},
        idempotency_key=f"skill-workflow-{run_id}",
    )
    db.add(row)
    contribution(db, actor, run.research_case_id, "workflow_results_saved", run_id)
    db.commit()
    return {"id": row.id}
