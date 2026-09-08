"""Versioned project material drafts using existing capability run snapshots."""

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.research import DbSession, ReaderActor, _case_permission, require_reader_write_access
from app.models import CapabilityConfig, CapabilityRun, CapabilityRunTarget, CapabilityTemplate, ResearchCase
from app.services import research_materials as materials
from app.services.reader_access import record_contribution

router = APIRouter(prefix="/api/v1/reader", tags=["project-materials"])


class MaterialReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: str = Field(
        pattern="^(document_version|event_mention|structured_observation_version|field_material|country_profile)$"
    )
    object_id: int = Field(gt=0)


class MaterialDraftPayload(BaseModel):
    instruction: str = Field(default="", max_length=12000)
    model_config = ConfigDict(extra="forbid")
    material_type: str
    references: list[MaterialReference] = Field(min_length=1, max_length=150)
    idempotency_key: str = Field(min_length=8, max_length=64)
    previous_target_id: int | None = Field(default=None, gt=0)
    comparison_dimensions: list[Annotated[str, Field(min_length=1, max_length=60)]] = Field(
        default_factory=lambda: ["政策目标", "政策工具", "执行主体"], max_length=8
    )


def _scope(case):
    return materials.digest(
        {"scope": case.scope, "brief_revision": case.brief_revision, "question": case.research_question}
    )


def _validate(document, case_id, db, actor):
    from app.api.project_workspace import _usable_output_evidence, _validate_output_citations

    _validate_output_citations(document.get("citations", []), _usable_output_evidence(case_id, db, actor))
    if document.get("scope_fingerprint") != _scope(db.get(ResearchCase, case_id)):
        raise HTTPException(409, "项目范围或研究问题已改变，请重新整理材料")


@router.get("/material-types")
def material_types():
    return {"items": materials.catalog()}


@router.get("/research-cases/{case_id}/material-evidence")
def material_evidence(case_id: int, db: DbSession, actor: ReaderActor):
    from app.api.project_workspace import _usable_output_evidence

    _case_permission(db, case_id, actor, "view")
    return {"items": _usable_output_evidence(case_id, db, actor)}


@router.get("/research-cases/{case_id}/material-drafts")
def list_drafts(case_id: int, db: DbSession, actor: ReaderActor):
    _case_permission(db, case_id, actor, "view")
    saved = set(db.scalars(select(CapabilityRunTarget.capability_run_id)))
    items = []
    for run in db.scalars(
        select(CapabilityRun)
        .where(CapabilityRun.research_case_id == case_id)
        .order_by(CapabilityRun.id.desc())
    ):
        doc = (run.output or {}).get("document", {})
        if run.id in saved or not doc.get("material_type"):
            continue
        from app.services import field_access

        try:
            field_access.enforce_run_access(db, run, actor)
        except field_access.FieldAccessDenied:
            items.append({"run_id": run.id, "title": None, "availability": "needs_rights_review"})
            continue
        try:
            _validate(doc, case_id, db, actor)
        except HTTPException:
            items.append({"run_id": run.id, "title": doc["title"], "availability": "needs_review"})
            continue
        items.append({"run_id": run.id, "document": doc, "availability": "available"})
    return {"items": items}


@router.post("/research-cases/{case_id}/material-drafts", dependencies=[Depends(require_reader_write_access)])
def create_draft(case_id: int, payload: MaterialDraftPayload, db: DbSession, actor: ReaderActor):
    from app.api.project_workspace import _usable_output_evidence

    _case_permission(db, case_id, actor, "run_skill")
    case = db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    kind = materials.normalize_type(payload.material_type)
    if not kind:
        raise HTTPException(422, "未知材料类型")
    keys = {(r.object_type, r.object_id) for r in payload.references}
    if len(keys) != len(payload.references):
        raise HTTPException(422, "引用列表不能重复")
    request = {
        "type": kind,
        "references": sorted(keys),
        "previous": payload.previous_target_id,
        "dimensions": payload.comparison_dimensions,
    }
    if payload.instruction:
        request["instruction"] = payload.instruction
    request_hash = materials.digest(request)
    for run in db.scalars(select(CapabilityRun).where(CapabilityRun.research_case_id == case_id)):
        if run.input_snapshot.get("material_request_key") == payload.idempotency_key:
            if run.input_snapshot.get("material_request_hash") != request_hash:
                raise HTTPException(409, "同一请求标识不能用于不同材料或引用")
            from app.services.field_access import enforce_run_access

            enforce_run_access(db, run, actor)
            _validate(run.output["document"], case_id, db, actor)
            return {"run_id": run.id, "document": run.output["document"], "idempotent_replay": True}
    available = {(r["object_type"], r["object_id"]): r for r in _usable_output_evidence(case_id, db, actor)}
    if not keys <= available.keys():
        raise HTTPException(409, "所选资料未采用、超出项目范围或引用已失效，请重新选择")
    selected = [available[key] for key in sorted(keys)]
    previous = None
    if payload.previous_target_id:
        previous = db.get(CapabilityRunTarget, payload.previous_target_id)
        prior_run = db.get(CapabilityRun, previous.capability_run_id) if previous else None
        if not prior_run or prior_run.research_case_id != case_id:
            raise HTTPException(404, "上一版本不属于当前项目")
        from app.services.field_access import enforce_run_access

        enforce_run_access(db, prior_run, actor)
        previous_doc = previous.artifact_snapshot.get("output", {}).get("document", {})
        if previous_doc.get("material_type") != kind:
            raise HTTPException(409, "新版本必须保持原材料类型")
        _validate(previous_doc, case_id, db, actor)
        if keys != {(c["object_type"], c["object_id"]) for c in previous_doc["citations"]}:
            raise HTTPException(409, "修订原稿需保留原引用；变更引用请整理为新材料")
    citations = [
        {
            **row["snapshot"],
            "number": number,
            "object_type": row["object_type"],
            "object_id": row["object_id"],
        }
        for number, row in enumerate(selected, 1)
    ]
    field_output = None
    if not previous and kind == "interview_minutes":
        raise HTTPException(422, "新田野成果请从 S05 工作流整理为研究备忘录；历史稿可创建修订")
    document = (
        dict(previous_doc)
        if previous
        else materials.build_document(
            kind,
            case,
            citations,
            field_output=field_output,
            comparison_dimensions=payload.comparison_dimensions,
            instruction=payload.instruction,
        )
    )
    document.update(
        citations=citations,
        scope_fingerprint=_scope(case),
        generated_at=datetime.now(UTC).isoformat(),
        created_by=actor.id,
        created_by_name=actor.display_name,
        previous_target_id=payload.previous_target_id,
        writing_instruction=payload.instruction,
        editorial_status="draft",
        draft_revision=1,
        comparison_dimensions=payload.comparison_dimensions,
    )
    document.pop("version", None)
    if len(document["markdown"]) > 50000:
        raise HTTPException(422, "本次材料超过正文上限，请减少所选引用后重试")
    # A dedicated internal template records the material executor, not a fictitious digest run.
    template = db.scalar(select(CapabilityTemplate).where(CapabilityTemplate.slug == "project-material"))
    if not template:
        template = CapabilityTemplate(
            slug="project-material",
            name="整理研究材料",
            version="1.0",
            status="active",
            default_config={"catalog_visibility": "internal_workflow"},
        )
        db.add(template)
        db.flush()
    config = db.scalar(
        select(CapabilityConfig).where(
            CapabilityConfig.template_id == template.id,
            CapabilityConfig.research_case_id == case_id,
            CapabilityConfig.owner_id == actor.id,
        )
    )
    if not config:
        config = CapabilityConfig(
            template_id=template.id,
            owner_id=actor.id,
            research_case_id=case_id,
            scope_type="research_case",
            name="研究材料整理",
            config={},
        )
        db.add(config)
        db.flush()
    run = CapabilityRun(
        config_id=config.id,
        research_case_id=case_id,
        requested_by=actor.id,
        status="succeeded",
        review_status="pending",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        output={"document": document},
        execution_summary={
            "executor": "research-materials",
            "type": kind,
            "generation_status": document["generation_status"],
        },
        input_snapshot={
            "material_request_key": payload.idempotency_key,
            "material_request_hash": request_hash,
            "selection": request,
            "scope_fingerprint": _scope(case),
        },
    )
    _validate(document, case_id, db, actor)
    db.add(run)
    db.flush()
    db.commit()
    return {"run_id": run.id, "document": document}


class EditMaterialPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    markdown: str = Field(min_length=1, max_length=50000)
    expected_revision: int = Field(ge=1)


def _validate_markdown(markdown, document):
    cited = {int(n) for n in re.findall(r"(?<!\\)\[(\d+)\](?!\()", markdown)}
    allowed = {c["number"] for c in document["citations"]}
    if not cited <= allowed:
        raise HTTPException(422, "正文含未登记的引用编号，请使用当前引用列表")


@router.patch(
    "/research-cases/{case_id}/material-drafts/{run_id}", dependencies=[Depends(require_reader_write_access)]
)
def edit_draft(case_id: int, run_id: int, payload: EditMaterialPayload, db: DbSession, actor: ReaderActor):
    _case_permission(db, case_id, actor, "edit")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    run = db.get(CapabilityRun, run_id)
    if not run or run.research_case_id != case_id or not run.output.get("document", {}).get("material_type"):
        raise HTTPException(404, "材料草稿不存在")
    if db.scalar(select(CapabilityRunTarget.id).where(CapabilityRunTarget.capability_run_id == run.id)):
        raise HTTPException(409, "已保存材料不可覆盖，请创建新版本")
    from app.services.field_access import enforce_run_access

    enforce_run_access(db, run, actor)
    document = run.output["document"]
    _validate(document, case_id, db, actor)
    _validate_markdown(payload.markdown, document)
    if document.get("draft_revision", 1) != payload.expected_revision:
        if document["title"] == payload.title and document["markdown"] == payload.markdown:
            return {"run_id": run.id, "document": document}
        raise HTTPException(409, "草稿已被更新，请重新打开后核对")
    document = {
        **document,
        "title": payload.title,
        "markdown": payload.markdown,
        "draft_revision": payload.expected_revision + 1,
        "edited_by": actor.id,
        "edited_by_name": actor.display_name,
    }
    run.output = {**run.output, "document": document}
    db.commit()
    return {"run_id": run.id, "document": document}


def confirm_material(case_id, run, payload, db, actor):
    _case_permission(db, case_id, actor, "confirm")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    from app.services.field_access import enforce_run_access

    enforce_run_access(db, run, actor)
    document = run.output["document"]
    _validate(document, case_id, db, actor)
    _validate_markdown(payload.markdown, document)
    previous = db.scalar(
        select(CapabilityRunTarget).where(
            CapabilityRunTarget.capability_run_id == run.id, CapabilityRunTarget.target_key == str(case_id)
        )
    )
    key_owner = db.scalar(
        select(CapabilityRunTarget).where(CapabilityRunTarget.idempotency_key == payload.idempotency_key)
    )
    if key_owner and (not previous or key_owner.id != previous.id):
        raise HTTPException(409, "保存请求标识已被使用")
    if previous:
        saved = previous.artifact_snapshot["output"]["document"]
        if saved["title"] != payload.title or saved["markdown"] != payload.markdown:
            raise HTTPException(409, "该草稿已保存；修改请创建新版本")
        return {"target_id": previous.id, "version": saved["version"], "idempotent_replay": True}
    parent = (
        db.get(CapabilityRunTarget, document.get("previous_target_id"))
        if document.get("previous_target_id")
        else None
    )
    root_id = (
        (parent.artifact_snapshot["output"]["document"].get("material_root_id") or parent.id)
        if parent
        else None
    )
    versions = [
        r.artifact_snapshot.get("output", {}).get("document", {}).get("version", 1)
        for r in db.scalars(
            select(CapabilityRunTarget).join(CapabilityRun).where(CapabilityRun.research_case_id == case_id)
        )
        if root_id
        and (
            r.id == root_id
            or r.artifact_snapshot.get("output", {}).get("document", {}).get("material_root_id") == root_id
        )
    ]
    version = max(versions, default=0) + 1
    saved = {
        **document,
        "title": payload.title,
        "markdown": payload.markdown,
        "version": version,
        "material_root_id": root_id,
        "editorial_status": "researcher_confirmed",
        "edited_by": actor.id,
        "edited_by_name": actor.display_name,
    }
    target = CapabilityRunTarget(
        capability_run_id=run.id,
        target_type="research_case",
        target_key=str(case_id),
        idempotency_key=payload.idempotency_key,
        confirmed_by=actor.id,
        artifact_snapshot={
            "output": {"document": saved},
            "input_snapshot": run.input_snapshot,
            "confirmed_status": run.status,
        },
    )
    db.add(target)
    db.flush()
    if not root_id:
        saved = {**saved, "material_root_id": target.id}
        target.artifact_snapshot = {**target.artifact_snapshot, "output": {"document": saved}}
    run.review_status = "approved"
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="material_confirmed",
        object_type="research_material",
        object_key=target.id,
        details={"material_type": saved["material_type"], "version": version},
    )
    db.commit()
    return {"target_id": target.id, "version": version}
