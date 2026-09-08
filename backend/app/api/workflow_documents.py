"""One readable document editor for S05 and S03, with reviewable AI revisions."""

import copy
import difflib
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.research import (
    DbSession,
    ReaderActor,
    _require_capability_run_access,
    require_internal_reader_access,
    require_reader_write_access,
)
from app.models import CapabilityRun, CapabilityRunRevision, CapabilityRunTarget
from app.models.skill_workflows import FieldAnnotation
from app.services import field_access, field_ai_access, workflow_runtime
from app.services import workflow_documents as documents

router = APIRouter(
    prefix="/api/v1/reader/workflow-documents",
    tags=["workflow-documents"],
    dependencies=[Depends(require_internal_reader_access)],
)
WRITE = [Depends(require_reader_write_access)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RevisionInput(Input):
    base_revision_id: int = Field(gt=0)


class Edit(RevisionInput):
    title: str = Field(min_length=1, max_length=240)
    markdown: str = Field(min_length=1, max_length=200_000)


class Rewrite(RevisionInput):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    instruction: str = Field(min_length=2, max_length=1500)
    operation: Literal["compress", "reorganize", "compare", "polish"]


class RewriteResult(Input):
    paragraph: str = Field(min_length=1, max_length=15_000)
    evidence_ids: list[int] = Field(max_length=60)


class Accept(RevisionInput):
    proposal_id: int = Field(gt=0)


def run_access(db, run_id, actor, action="view"):
    run = db.scalar(select(CapabilityRun).where(CapabilityRun.id == run_id).with_for_update())
    if not run or run.status != "succeeded":
        raise HTTPException(404, "已完成工作流不存在")
    _require_capability_run_access(db, run, actor, action)
    if run.output.get("type") not in {"field_research_workflow", "policy_tracking_workflow"}:
        raise HTTPException(422, "此运行不是 S03 / S05 工作流")
    return run


def current_revision(db, run_id, base):
    latest = documents.latest(db, run_id)
    if not latest or latest.id != base:
        raise HTTPException(409, "正文已被其他操作修改，请刷新并保留本地输入")
    return latest


def payload(row):
    return {
        "id": row.id,
        "revision_no": row.revision_no,
        "state": row.artifact["state"],
        "document": row.artifact["document"],
        "created_at": row.created_at,
        "created_by": row.created_by,
        "note": row.note,
    }


@router.get("/{run_id}")
def read(run_id: int, db: DbSession, actor: ReaderActor, response: Response):
    run_access(db, run_id, actor)
    current = documents.latest(db, run_id)
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "current": payload(current) if current else None,
        "history": [
            {k: v for k, v in payload(row).items() if k != "document"}
            for row in documents.revisions(db, run_id)
        ],
    }


@router.get("/{run_id}/revisions/{revision_id}")
def read_revision(run_id: int, revision_id: int, db: DbSession, actor: ReaderActor, response: Response):
    run_access(db, run_id, actor)
    row = db.get(CapabilityRunRevision, revision_id)
    if not row or row.capability_run_id != run_id or row.artifact.get("kind") != documents.DOCUMENT_KIND:
        raise HTTPException(404, "文稿版本不存在")
    response.headers["Cache-Control"] = "private, no-store"
    return payload(row)


@router.post("/{run_id}/prepare", dependencies=WRITE)
def prepare(run_id: int, db: DbSession, actor: ReaderActor):
    run = run_access(db, run_id, actor, "revise")
    existing = documents.latest(db, run.id)
    if existing:
        return payload(existing)
    row = documents.append(
        db, run, actor.id, documents.initial_document(db, run), "draft", "依据实际材料形成首稿"
    )
    db.commit()
    return payload(row)


@router.post("/{run_id}/edit", dependencies=WRITE)
def edit(run_id: int, body: Edit, db: DbSession, actor: ReaderActor):
    run = run_access(db, run_id, actor, "revise")
    parent = current_revision(db, run_id, body.base_revision_id)
    document = copy.deepcopy(parent.artifact["document"])
    try:
        documents.validate_body(document, body.markdown)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    document.update(title=body.title, markdown=body.markdown)
    row = documents.append(db, run, actor.id, document, "draft", "人工编辑正文", parent_id=parent.id)
    db.commit()
    return payload(row)


@router.post("/{run_id}/refresh-evidence", dependencies=WRITE)
def refresh(run_id: int, body: RevisionInput, db: DbSession, actor: ReaderActor):
    run = run_access(db, run_id, actor, "revise")
    parent = current_revision(db, run_id, body.base_revision_id)
    document = copy.deepcopy(parent.artifact["document"])
    fresh = documents.initial_document(db, run)
    document["evidence"], document["materials"] = fresh["evidence"], fresh["materials"]
    try:
        documents.validate_body(document, document["markdown"])
    except ValueError as exc:
        raise HTTPException(409, "已拒绝的证据仍被正文引用，请先删除相应引用") from exc
    row = documents.append(
        db, run, actor.id, document, "draft", "同步当前证据审核；正文保留", parent_id=parent.id
    )
    db.commit()
    return payload(row)


@router.post("/{run_id}/proposals", dependencies=WRITE)
def propose(run_id: int, body: Rewrite, db: DbSession, actor: ReaderActor):
    run = run_access(db, run_id, actor, "run_skill")
    parent = current_revision(db, run_id, body.base_revision_id)
    document = copy.deepcopy(parent.artifact["document"])
    markdown = document["markdown"]
    if body.end <= body.start or body.end > len(markdown) or body.end - body.start > 12_000:
        raise HTTPException(422, "请选择正文中不超过 12000 字的段落")
    selected = markdown[body.start : body.end]
    evidence = document["evidence"]
    # Field-derived text is handled exclusively under project-specific AI grants.
    with ExitStack() as stack:
        for material_id in field_access.run_material_ids(db, run):
            stack.enter_context(field_ai_access.authorized_source(db, run, material_id, actor_id=actor.id))
        result = workflow_runtime.invoke(
            RewriteResult,
            {
                "selected_paragraph": selected,
                "operation": body.operation,
                "instruction": body.instruction,
                "evidence": evidence,
            },
            "你是研究文稿修订助手。只修订选定段落，不写完整报告。"
            "引文原文不可改写或作为普通正文复制；引用使用 [E数字]。"
            "保留数字、否定和不确定性，不补充输入中没有的事实。"
            "新增判断须引用支持证据；无直接支持的判断明确写‘待核实’。"
            "evidence_ids 列出修订后实际引用的证据编号。",
        )
    known = {x["id"] for x in evidence}
    used = {int(x) for x in documents.CITATION.findall(result.paragraph)}
    if not set(result.evidence_ids) <= known or used != set(result.evidence_ids):
        raise HTTPException(422, "修订引用校验未通过，未形成候选版本")
    paragraph = result.paragraph
    if not used and "待核实" not in paragraph:
        paragraph = "待核实：" + paragraph
    # Exact source quotations cannot be smuggled into the editable body for future rewriting.
    if any(len(x.get("quote", "")) > 20 and x["quote"] in paragraph for x in evidence):
        raise HTTPException(422, "修订包含原文引句，请改用证据编号引用")
    document["markdown"] = markdown[: body.start] + paragraph + markdown[body.end :]
    documents.validate_body(document, document["markdown"])
    row = documents.append(db, run, actor.id, document, "proposal", body.instruction, parent_id=parent.id)
    db.commit()
    return {
        **payload(row),
        "base_revision_id": parent.id,
        "before": selected,
        "after": paragraph,
        "diff": "\n".join(
            difflib.unified_diff(
                selected.splitlines(),
                paragraph.splitlines(),
                fromfile="修改前",
                tofile="候选修订",
                lineterm="",
            )
        ),
    }


@router.post("/{run_id}/accept", dependencies=WRITE)
def accept(run_id: int, body: Accept, db: DbSession, actor: ReaderActor):
    run = run_access(db, run_id, actor, "revise")
    parent = current_revision(db, run_id, body.base_revision_id)
    proposal = db.get(CapabilityRunRevision, body.proposal_id)
    if (
        not proposal
        or proposal.capability_run_id != run_id
        or proposal.artifact.get("state") != "proposal"
        or proposal.artifact.get("parent_id") != parent.id
    ):
        raise HTTPException(409, "候选修订已失效，请基于当前正文重新修订")
    row = documents.append(
        db, run, actor.id, proposal.artifact["document"], "draft", "接受 AI 修订", parent_id=parent.id
    )
    db.commit()
    return payload(row)


@router.post("/{run_id}/confirm", dependencies=WRITE)
def confirm(run_id: int, body: RevisionInput, db: DbSession, actor: ReaderActor):
    run = run_access(db, run_id, actor, "confirm")
    parent = current_revision(db, run_id, body.base_revision_id)
    if parent.artifact["state"] == "confirmed":
        return payload(parent)
    document = parent.artifact["document"]
    used = {int(x) for x in documents.CITATION.findall(document["markdown"])}
    for evidence in document["evidence"]:
        if evidence["id"] not in used or not evidence.get("annotation_id"):
            continue
        annotation = db.get(FieldAnnotation, evidence["annotation_id"])
        newest = db.scalar(
            select(FieldAnnotation)
            .where(FieldAnnotation.segment_id == annotation.segment_id)
            .order_by(FieldAnnotation.revision_no.desc())
            .limit(1)
        )
        if annotation.id != newest.id or annotation.status not in {"confirmed", "modified"}:
            raise HTTPException(409, f"请先在证据页复核 E{evidence['id']}，并同步证据状态")
    from app.models.project_workspace import ProjectEvidence
    from app.models.skill_workflows import TrackingReview

    for evidence in document["evidence"]:
        if evidence["id"] not in used or not evidence.get("observation_id"):
            continue
        oid = evidence["observation_id"]
        decision = db.scalar(
            select(ProjectEvidence.decision).where(
                ProjectEvidence.research_case_id == run.research_case_id,
                ProjectEvidence.object_type == "tracking_observation",
                ProjectEvidence.object_id == oid,
            )
        )
        review = db.scalar(
            select(TrackingReview)
            .where(TrackingReview.observation_id == oid)
            .order_by(TrackingReview.revision_no.desc())
            .limit(1)
        )
        if decision != "accepted" and (not review or review.payload.get("decision") != "saved"):
            raise HTTPException(409, f"请先审核采用观察证据 E{evidence['id']}")
    row = documents.append(
        db, run, actor.id, document, "confirmed", "研究者确认不可变成果版本", parent_id=parent.id
    )
    # Each immutable confirmation receives its own delivery key; no previous target is overwritten.
    target = CapabilityRunTarget(
        capability_run_id=run.id,
        target_type="research_case",
        target_key=f"{run.research_case_id}:document:{row.id}",
        idempotency_key=f"workflow-doc-{row.id}",
        confirmed_by=actor.id,
        confirmed_at=datetime.now(UTC),
        artifact_snapshot={
            "type": run.output["type"],
            "title": document["title"],
            "workflow_document_revision_id": row.id,
            "saved_revision_no": row.revision_no,
            "source_run_id": run.id,
            "document": document,
        },
    )
    db.add(target)
    db.commit()
    return {**payload(row), "target_id": target.id}


@router.get("/{run_id}/revisions/{revision_id}/export")
def export(
    run_id: int,
    revision_id: int,
    db: DbSession,
    actor: ReaderActor,
    format: Literal["markdown", "docx", "csv"] = "markdown",
):
    run_access(db, run_id, actor, "confirm")
    row = db.get(CapabilityRunRevision, revision_id)
    if (
        not row
        or row.capability_run_id != run_id
        or row.artifact.get("kind") != documents.DOCUMENT_KIND
        or row.artifact.get("state") != "confirmed"
    ):
        raise HTTPException(409, "请选择已确认的不可变成果版本导出")
    document = row.artifact["document"]
    content, mime, ext = (
        (
            documents.docx_export(document),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        )
        if format == "docx"
        else (documents.csv_export(document), "text/csv; charset=utf-8", "csv")
        if format == "csv"
        else (documents.markdown_export(document), "text/markdown; charset=utf-8", "md")
    )
    return Response(
        content=content,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="research-{run_id}-v{row.revision_no}.{ext}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
