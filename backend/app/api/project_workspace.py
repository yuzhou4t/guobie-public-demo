"""Human-gated project research, reusing Reader country queries and output storage."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import socket
import threading
import time
import traceback
import uuid
from collections import deque
from contextvars import copy_context
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api import research as reader
from app.api.research import DbSession, ReaderActor, require_reader_write_access
from app.models import (
    AgentRun,
    CapabilityConfig,
    CapabilityRun,
    CapabilityRunTarget,
    CapabilityTemplate,
    Document,
    DocumentVersion,
    EventMention,
    ProjectEvidence,
    ProjectPlan,
    ProjectTask,
    ResearchCase,
    ResearchCaseDocument,
    ResearchCaseEvent,
    ResearchEntity,
    ResearchEvent,
    Source,
)
from app.services import project_research as workflow
from app.services.frontier_relations import frontier_relations
from app.services.project_candidates import structured_candidates
from app.services.project_direction_topics import direction_groups
from app.services.reader_access import record_contribution
from app.services.research_capabilities import _published_precision, run_capability
from app.services.research_taxonomy import DOMAINS
from app.services.research_trends import research_trends

router = APIRouter(prefix="/api/v1/reader", tags=["project-workspace"])

PUBLIC_PLAN_PROCESS = (
    {"stage": "scope", "label": "理解范围", "summary": "核对研究对象、时间范围与材料边界"},
    {"stage": "questions", "label": "整理计划", "summary": "整理核心问题、成果要求与待确认事项"},
    {"stage": "search", "label": "撰写草稿", "summary": "形成候选关键词、检索路径与资料分组"},
    {"stage": "constraints", "label": "检查约束", "summary": "检查范围、证据条件与执行边界"},
)


class BriefPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    original: str = Field(min_length=1, max_length=6000)
    purpose: str = Field(min_length=1, max_length=2000)
    scope: str = Field(min_length=1, max_length=2000)
    outcome: str = Field(default="团队周报", max_length=1000)


@router.patch("/research-cases/{case_id}/brief", dependencies=[Depends(require_reader_write_access)])
def update_brief(case_id: int, payload: BriefPayload, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "edit")
    case = db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    if case.brief_revision != payload.expected_revision:
        raise HTTPException(409, "项目说明已被更新，请重新读取")
    case.brief_original = payload.original
    case.brief_confirmed = {
        **(case.brief_confirmed or {}),
        **payload.model_dump(exclude={"original", "expected_revision"}),
    }
    case.research_question = payload.purpose
    case.brief_revision += 1
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="brief_confirmed",
        object_type="research_case",
        object_key=case_id,
        details={"revision": case.brief_revision, "brief": workflow.brief(case)},
    )
    db.commit()
    return workflow.brief(case)


@router.delete("/research-cases/{case_id}", dependencies=[Depends(require_reader_write_access)])
def delete_project(case_id: int, db: DbSession, actor: ReaderActor):
    case = db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    if case is None:
        raise HTTPException(404, "项目不存在")
    if case.owner_id != actor.id:
        raise HTTPException(403, "只有项目负责人可以删除项目")
    if (case.scope or {}).get("deleted_at"):
        return {"deleted": True, "id": case_id}
    tasks = db.scalars(select(ProjectTask).where(ProjectTask.research_case_id == case_id))
    busy = any(task.status == "running" or workflow.plan_in_progress(task) for task in tasks)
    running_agent = db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.scope_type == "topic",
            AgentRun.scope_key == str(case_id),
            AgentRun.status.in_(["queued", "running"]),
        )
        .limit(1)
    )
    running_skill = db.scalar(
        select(CapabilityRun.id)
        .where(
            CapabilityRun.research_case_id == case_id,
            CapabilityRun.status.in_(["queued", "running"]),
        )
        .limit(1)
    )
    if busy or running_agent or running_skill:
        raise HTTPException(409, "项目中有任务正在运行，请先停止后再删除")
    case.scope = {**(case.scope or {}), "deleted_at": datetime.now(UTC).isoformat()}
    case.status = "archived"
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="project_deleted",
        object_type="research_case",
        object_key=case_id,
        details={"title": case.title, "retained": "project_records_and_country_materials"},
    )
    db.commit()
    return {"deleted": True, "id": case_id}


@router.get("/research-cases/{case_id}/brief/overview")
def project_overview(case_id: int, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "view")
    case = db.get(ResearchCase, case_id)
    deleted = workflow.deleted_conversation_ids(db, case_id)
    plan = db.scalar(
        select(ProjectPlan)
        .join(ProjectTask, ProjectTask.id == ProjectPlan.task_id)
        .where(
            ProjectTask.research_case_id == case_id,
            ProjectTask.id.not_in(deleted),
            ProjectPlan.content["planner"].as_string() == "assistant",
        )
        .order_by(ProjectPlan.created_at.desc(), ProjectPlan.id.desc())
        .limit(1)
    )
    overview = None
    if plan:
        content = plan.content
        overview = {
            "summary": content.get("summary", ""),
            "steps": content.get("steps", []),
            "evidence_gaps": content.get("evidence_gaps", []),
            "output_type": content.get("output_type", "待讨论"),
            "task_id": plan.task_id,
            "plan_revision": plan.revision,
            "updated_at": plan.created_at.isoformat(),
            "brief_changed": content.get("brief_snapshot", {}).get("revision") != case.brief_revision,
        }
    return {"brief": workflow.brief(case), "overview": overview}


@router.post("/research-cases/{case_id}/brief/suggestion")
def suggest_brief(case_id: int, payload: BriefPayload, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "edit")
    # Suggestions are never written without the explicit PATCH above.
    runtime = workflow.get_structured_agent_runtime(workflow.get_settings())
    result = payload.model_dump()
    if runtime:
        try:
            result = BriefPayload.model_validate(
                runtime._invoke_json(
                    instructions=(
                        "整理用户的项目说明，保持原意，不增加研究事实。"
                        "原始说明和 expected_revision 原样保留。返回 purpose、scope、outcome 的清晰表达。"
                    ),
                    prompt=payload.model_dump_json(),
                    schema=BriefPayload.model_json_schema(),
                )
            ).model_dump()
        except Exception as exc:
            raise HTTPException(503, "AI 整理暂不可用，原文已保留，可直接编辑") from exc
    result["original"] = payload.original
    result["expected_revision"] = payload.expected_revision
    return {"suggestion": result, "saved": False, "mode": "assistant" if runtime else "rules"}


def _task(db, case_id, task_id):
    if task_id in workflow.deleted_conversation_ids(db, case_id):
        raise HTTPException(404, "对话已删除")
    task = db.get(ProjectTask, task_id)
    if not task or task.research_case_id != case_id:
        raise HTTPException(404, "研究任务不存在或不属于此项目")
    return task


def _event(task_id, name, payload):
    data = json.dumps({"conversation_id": task_id, "payload": payload}, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {data}\n\n"


def _background_plan_events(events):
    """The response subscribes to a bounded feed; disconnect never cancels execution."""
    pending = deque(maxlen=64)
    changed = threading.Condition()
    finished = False

    def execute():
        nonlocal finished
        try:
            for event in events:
                with changed:
                    pending.append(event)
                    changed.notify()
        finally:
            with changed:
                finished = True
                changed.notify_all()

    threading.Thread(target=copy_context().run, args=(execute,), daemon=True, name="project-plan").start()

    def subscribe():
        while True:
            with changed:
                changed.wait_for(lambda: pending or finished)
                if not pending:
                    return
                event = pending.popleft()
            yield event

    return subscribe()


def _configs(db, case_id, actor, ids):
    result = []
    for config_id in ids:
        config = db.get(CapabilityConfig, config_id)
        template = db.get(CapabilityTemplate, config.template_id) if config else None
        if not config or not reader._capability_config_visible(db, config, actor, "run_skill"):
            raise HTTPException(403, "无法使用这个 Skill")
        if config.research_case_id and config.research_case_id != case_id:
            raise HTTPException(403, "Skill 不属于当前项目")
        if (
            config.status not in {"active", "published"}
            or not template
            or template.validation_status != "verified"
        ):
            raise HTTPException(409, "Skill 尚未验证或已停用")
        result.append(config)
    return result


def stream_plan(case_id, payload, db, actor):
    reader._case_permission(db, case_id, actor, "edit")
    if payload.online_mode != "off":
        raise HTTPException(422, "项目研究仅使用国别空间数据库")
    task_id = payload.conversation_id or str(uuid.uuid4())
    # Serialize creation as well as updates, including two requests for a new ID.
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    task = db.scalar(
        select(ProjectTask)
        .where(ProjectTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            payload.model_dump(exclude={"request_id", "conversation_id"}), sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
    if task:
        _task(db, case_id, task_id)
        prior_request = next(
            (m for m in task.messages if payload.request_id and m.get("request_id") == payload.request_id),
            None,
        )
        if prior_request:
            if (
                prior_request.get("request_fingerprint")
                and prior_request["request_fingerprint"] != fingerprint
            ):
                raise HTTPException(409, "同一请求标识不能用于不同内容")
            db.commit()
            return StreamingResponse(
                iter([_event(task_id, "project.task", workflow.task_payload(db, task))]),
                media_type="text/event-stream",
            )
        if task.status == "running" or workflow.plan_in_progress(task):
            raise HTTPException(409, "任务正在运行，请先停止")
        if payload.expected_revision != task.revision:
            raise HTTPException(409, "计划已更新，请重新读取后继续讨论")
    else:
        existing = db.scalar(select(AgentRun).where(AgentRun.conversation_id == task_id))
        if existing and (existing.scope_type != "topic" or existing.scope_key != str(case_id)):
            raise HTTPException(409, "对话属于其他研究范围")
        task = ProjectTask(
            id=task_id,
            research_case_id=case_id,
            created_by=actor.id,
            title=workflow.short_title("", db.get(ResearchCase, case_id).title),
            revision=0,
            messages=[],
        )
        db.add(task)
        db.flush()
    references = reader._validated_project_references(db, db.get(ResearchCase, case_id), payload.references)
    configs = _configs(db, case_id, actor, payload.capability_config_ids)
    previous = workflow.get_plan(db, task)
    if payload.answers and previous:
        allowed = {q["id"] for q in previous.content.get("questions", [])} | set(
            previous.content.get("answers", {})
        )
        if set(payload.answers) - allowed:
            raise HTTPException(422, "答案不对应当前计划问题")
    operation_id = str(uuid.uuid4())
    workflow.append_message(
        task,
        "user",
        payload.question,
        answers=payload.answers,
        references=references,
        reference_snapshots=[
            _material_snapshot(
                db, int(ref["id"]), (db.get(ResearchCase, case_id).scope or {}).get("country_iso3")
            )
            for ref in references
            if ref["type"] == "document_version"
        ],
        request_id=payload.request_id,
        request_fingerprint=fingerprint,
        operation_id=operation_id,
    )
    task.status = "planning"
    task.error = None
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    config_ids = [c.id for c in configs]
    start_revision = task.revision

    def owns_operation(current):
        return (
            workflow.plan_in_progress(current)
            and current.revision == start_revision
            and current.messages[-1].get("operation_id") == operation_id
        )

    def lock_current(session):
        return session.scalar(
            select(ProjectTask)
            .where(ProjectTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def events():
        first_stage = PUBLIC_PLAN_PROCESS[0]
        yield _event(
            task_id,
            "run.started",
            {**first_stage, "message": f"正在{first_stage['label']}，尚未查询资料"},
        )
        with factory() as session:
            current = _task(session, case_id, task_id)
            try:
                if not owns_operation(current):
                    yield _event(task_id, "project.task", workflow.task_payload(session, current))
                    return
                updates = queue.Queue(maxsize=64)

                def publish(kind, value):
                    try:
                        updates.put_nowait((kind, value))
                    except queue.Full:
                        updates.get_nowait()
                        updates.put_nowait((kind, value))

                cancelled = threading.Event()

                def generate():
                    try:
                        with factory() as worker_session:
                            worker_task = _task(worker_session, case_id, task_id)
                            worker_case = worker_session.get(ResearchCase, case_id)
                            worker_plan = workflow.get_plan(worker_session, worker_task)
                            worker_configs = [worker_session.get(CapabilityConfig, cid) for cid in config_ids]
                            # All planning inputs are column snapshots, with no lazy relationships.
                            worker_session.expunge_all()
                        value = workflow.make_plan(
                            worker_case,
                            worker_task,
                            worker_plan,
                            payload.answers,
                            payload.question,
                            worker_configs,
                            on_preview=lambda value: publish("preview", value),
                            cancelled=cancelled,
                        )
                        publish("done", value)
                    except Exception as error:
                        publish("error", error)

                threading.Thread(target=copy_context().run, args=(generate,), daemon=True).start()
                started = time.monotonic()
                last_check = started
                try:
                    yield _event(
                        task_id,
                        "project.progress",
                        {
                            "stage": "questions",
                            "message": "正在整理研究问题与计划草稿",
                            "elapsed_seconds": 0,
                        },
                    )
                    while True:
                        try:
                            kind, value = updates.get(timeout=1)
                        except queue.Empty:
                            kind, value = "heartbeat", None
                        if kind != "preview" or time.monotonic() - last_check >= 0.5:
                            session.expire_all()
                            current = _task(session, case_id, task_id)
                            last_check = time.monotonic()
                            if not owns_operation(current):
                                yield _event(task_id, "project.task", workflow.task_payload(session, current))
                                return
                        if kind == "done":
                            content = value
                            break
                        if kind == "error":
                            raise value
                        if kind == "preview":
                            yield _event(task_id, "project.plan.preview", value)
                        else:
                            yield _event(
                                task_id,
                                "project.progress",
                                {
                                    "stage": "questions",
                                    "message": "正在生成计划，尚未执行检索",
                                    "elapsed_seconds": int(time.monotonic() - started),
                                },
                            )
                finally:
                    cancelled.set()
                content["public_process"] = [dict(stage) for stage in PUBLIC_PLAN_PROCESS]
                content["prompt_snapshot"] = payload.question
                content["references"] = references
                content["reference_snapshots"] = current.messages[-1].get("reference_snapshots", [])
                final_stage = PUBLIC_PLAN_PROCESS[-1]
                yield _event(
                    task_id,
                    "project.progress",
                    {**final_stage, "message": f"正在{final_stage['label']}"},
                )
                current = lock_current(session)
                if not owns_operation(current):
                    session.commit()
                    yield _event(task_id, "project.task", workflow.task_payload(session, current))
                    return
                if content.get("planner") == "assistant" and not session.scalar(
                    select(ProjectPlan.id)
                    .where(
                        ProjectPlan.task_id == task_id,
                        ProjectPlan.content["planner"].as_string() == "assistant",
                    )
                    .limit(1)
                ):
                    current.title = workflow.short_title(
                        content.get("title", ""), session.get(ResearchCase, case_id).title
                    )
                current.revision += 1
                session.add(ProjectPlan(task_id=task_id, revision=current.revision, content=content))
                current.status = "planning" if content["questions"] else "awaiting_confirmation"
                workflow.append_message(
                    current, "assistant", content["summary"], plan_revision=current.revision
                )
                session.commit()
                yield _event(task_id, "project.task", workflow.task_payload(session, current))
            except Exception as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                logging.getLogger(__name__).error(
                    "Plan failed task=%s operation=%s type=%s sqlstate=%s frames=%s",
                    task_id,
                    operation_id,
                    type(exc).__name__,
                    getattr(getattr(exc, "orig", None), "sqlstate", None),
                    [(os.path.basename(frame.filename), frame.lineno, frame.name) for frame in frames],
                )
                session.rollback()
                current = lock_current(session)
                if owns_operation(current):
                    current.status = "failed"
                    current.error = (
                        str(exc.detail)
                        if isinstance(exc, HTTPException)
                        else "计划未完成，可重试；已保存原有讨论"
                    )
                session.commit()
                yield _event(task_id, "project.task", workflow.task_payload(session, current))

    return StreamingResponse(
        _background_plan_events(events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ExecutePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=64)


@router.post(
    "/research-cases/{case_id}/tasks/{task_id}/plans/{revision}/execute",
    dependencies=[Depends(require_reader_write_access)],
)
def execute_plan(
    case_id: int, task_id: str, revision: int, payload: ExecutePayload, db: DbSession, actor: ReaderActor
):
    reader._case_permission(db, case_id, actor, "edit")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    task = db.scalar(
        select(ProjectTask)
        .where(ProjectTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not task or task.research_case_id != case_id:
        raise HTTPException(404, "研究任务不存在或不属于此项目")
    plan = workflow.get_plan(db, task)
    if not plan or revision != task.revision:
        raise HTTPException(409, "请确认最新计划")
    workflow.validate_plan(db, task, plan, require_confirmed=False)
    if plan.execution_key == payload.idempotency_key:
        return StreamingResponse(
            iter([_event(task_id, "project.task", workflow.task_payload(db, task))]),
            media_type="text/event-stream",
        )
    if task.status == "running" or workflow.plan_in_progress(task):
        raise HTTPException(409, "检索正在进行")
    if task.status in {"review", "completed"}:
        raise HTTPException(409, "该计划已执行，需要重新检索请修订计划")
    task.status, task.error = "running", None
    plan.confirmed_by, plan.confirmed_at = actor.id, datetime.now(UTC)
    plan.execution_key = payload.idempotency_key
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    content = plan.content

    def lock_execution(session):
        current = session.scalar(
            select(ProjectTask)
            .where(ProjectTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current_plan = workflow.get_plan(session, current)
        if current_plan:
            session.refresh(current_plan)
        owned = bool(
            current.status == "running"
            and current.revision == revision
            and current_plan
            and current_plan.execution_key == payload.idempotency_key
        )
        return current, owned

    def events():
        yield _event(task_id, "run.started", {"message": "计划已确认，开始从国别空间检索"})
        with factory() as session:
            current = _task(session, case_id, task_id)
            try:
                terms = [term.strip()[:100] for term in content["search_terms"] if term.strip()][:16]
                now = datetime.now(UTC)
                start = now.replace(year=now.year - content["years"], day=min(now.day, 28))
                preexisting = {
                    (row.object_type, row.object_id)
                    for row in session.scalars(
                        select(ProjectEvidence).where(ProjectEvidence.research_case_id == case_id)
                    )
                }
                matches = {}
                retrieval = {
                    "started_at": now.isoformat(),
                    "country_iso3": content["country_iso3"],
                    "published_from": start.isoformat(),
                    "published_to": now.isoformat(),
                    "terms": terms,
                    "status": "running",
                    "raw_count": 0,
                    "queries": [],
                }

                def remember(kind, oid, snapshot, term, channel):
                    retrieval["raw_count"] += 1
                    snapshot = {**snapshot, "matched_terms": [term]}
                    evidence = workflow.add_candidate(session, case_id, task_id, kind, oid, snapshot)
                    key = (kind, oid)
                    if key not in matches:
                        matches[key] = {
                            "object_type": kind,
                            "object_id": oid,
                            "title": snapshot.get("title", "资料"),
                            "source_name": snapshot.get("source_name", ""),
                            "published_at": snapshot.get("published_at"),
                            "published_at_precision": snapshot.get("published_at_precision"),
                            "terms": [],
                            "channels": [],
                            "already_in_project": key in preexisting,
                            "decision_at_search": evidence.decision,
                        }
                    for field, value in (("terms", term), ("channels", channel)):
                        if value not in matches[key][field]:
                            matches[key][field].append(value)

                def save_retrieval():
                    retrieval["matches"] = list(matches.values())
                    retrieval["unique_count"] = len(matches)
                    retrieval["existing_count"] = sum(k in preexisting for k in matches)
                    retrieval["new_count"] = len(matches) - retrieval["existing_count"]
                    retrieval["new_pending_count"] = sum(
                        k not in preexisting and v["decision_at_search"] == "pending"
                        for k, v in matches.items()
                    )
                    current_plan = workflow.get_plan(session, current)
                    current_plan.content = {
                        **current_plan.content,
                        "retrieval": json.loads(json.dumps(retrieval, default=str)),
                    }

                for channel, label in [
                    ("frontier_research", "学术研究"),
                    ("policy_center", "政策资料"),
                    ("event_dynamics", "事件材料"),
                ]:
                    yield _event(task_id, "project.progress", {"message": f"正在查询国别空间 · {label}"})
                    current, owned = lock_execution(session)
                    if not owned:
                        session.commit()
                        break
                    workflow.validate_plan(session, current, workflow.get_plan(session, current))
                    for term in terms:
                        before_hits = retrieval["raw_count"]
                        rows = reader.search_materials(
                            session,
                            q=term,
                            country_iso3=content["country_iso3"],
                            channel=channel,
                            published_from=start,
                            published_to=now,
                            limit=50,
                        )["items"]
                        for row in rows:
                            snapshot = {
                                **row,
                                "country_iso3": content["country_iso3"],
                                "source_space": "country",
                                "country_url": (
                                    f"#/countries/{content['country_iso3']}/"
                                    + {"frontier_research": "research", "policy_center": "policies"}.get(
                                        channel, "events"
                                    )
                                ),
                                "locator": {"document_version_id": row["document_version_id"]},
                                "boundary": "书目与来源信息；不据此推断全文结论",
                            }
                            # Never copy abstracts or bodies into the project model context.
                            snapshot.pop("abstract", None)
                            snapshot.pop("body", None)
                            remember("document_version", row["document_version_id"], snapshot, term, channel)
                            for mention in session.scalars(
                                select(EventMention)
                                .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
                                .join(ResearchEntity, ResearchEntity.id == ResearchEvent.country_entity_id)
                                .where(
                                    EventMention.document_version_id == row["document_version_id"],
                                    EventMention.review_status == "confirmed",
                                    ResearchEvent.review_status == "reviewed",
                                    ResearchEntity.entity_type == "country",
                                    ResearchEntity.canonical_key == content["country_iso3"],
                                )
                            ):
                                remember(
                                    "event_mention",
                                    mention.id,
                                    {
                                        **snapshot,
                                        "title": session.get(ResearchEvent, mention.event_id).title,
                                        "source_document_title": row["title"],
                                        "locator": {
                                            **snapshot["locator"],
                                            "event_mention_id": mention.id,
                                            "event_id": mention.event_id,
                                            "mention_locator": mention.evidence_locator,
                                        },
                                        "country_url": (
                                            f"#/countries/{content['country_iso3']}/events/{mention.event_id}"
                                        ),
                                    },
                                    term,
                                    channel,
                                )
                        retrieval["queries"].append(
                            {
                                "channel": channel,
                                "label": label,
                                "term": term,
                                "count": retrieval["raw_count"] - before_hits,
                                "limit": 50,
                                "at_limit": len(rows) == 50,
                            }
                        )
                        yield _event(
                            task_id,
                            "project.progress",
                            {
                                "message": (
                                    f"{label} · {term}：{len(rows)} 条文档匹配，"
                                    f"累计 {len(matches)} 条去重资料"
                                ),
                                "query": retrieval["queries"][-1],
                            },
                        )
                    save_retrieval()
                    session.commit()
                yield _event(task_id, "project.progress", {"message": "正在查询国别空间 · 结构化数据"})
                current, owned = lock_execution(session)
                if owned:
                    workflow.validate_plan(session, current, workflow.get_plan(session, current))
                    catalog = reader.get_country_data_catalog(content["country_iso3"], session)
                    before_hits = retrieval["raw_count"]
                    for version_id, snapshot in structured_candidates(catalog, terms, start, now):
                        remember(
                            "structured_observation_version",
                            version_id,
                            snapshot,
                            " · ".join(terms),
                            "structured_data",
                        )
                    retrieval["queries"].append(
                        {
                            "channel": "structured_data",
                            "label": "结构化数据",
                            "term": " · ".join(terms),
                            "count": retrieval["raw_count"] - before_hits,
                            "at_limit": False,
                        }
                    )
                    if owned:
                        current.status = "review"
                        retrieval["status"] = "completed"
                        retrieval["finished_at"] = datetime.now(UTC).isoformat()
                        save_retrieval()
                        count = len(matches)
                        workflow.append_message(
                            current,
                            "assistant",
                            (
                                f"已从国别空间匹配 {retrieval['raw_count']} 条记录，去重后 {count} 条资料；"
                                f"其中 {retrieval['existing_count']} 条已在项目内，"
                                f"新增 {retrieval['new_pending_count']} 条待选。"
                                "请在项目资料库逐项审阅；未采用的资料不会进入成果。"
                            ),
                            evidence_count=count,
                        )
                session.commit()
                yield _event(task_id, "project.task", workflow.task_payload(session, current))
            except Exception as exc:
                session.rollback()
                current, owned = lock_execution(session)
                if owned:
                    current.status = "failed"
                    saved_plan = workflow.get_plan(session, current)
                    if saved_plan.content.get("retrieval"):
                        saved_plan.content = {
                            **saved_plan.content,
                            "retrieval": {**saved_plan.content["retrieval"], "status": "failed"},
                        }
                    current.error = (
                        str(exc.detail)
                        if isinstance(exc, HTTPException)
                        else "检索中断，已保存的候选可继续审阅；重试不会重复加入资料"
                    )
                session.commit()
                yield _event(task_id, "project.task", workflow.task_payload(session, current))

    return StreamingResponse(events(), media_type="text/event-stream")


def _material_snapshot(db, version_id, country):
    row = db.execute(
        select(DocumentVersion, Document, Source)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(DocumentVersion.id == version_id)
    ).first()
    if not row:
        return {}
    version, document, source = row
    return {
        "title": version.title or document.title,
        "source_name": source.name,
        "published_at": str(version.published_at or document.published_at or ""),
        "source_url": document.canonical_url,
        "country_iso3": country,
        "source_space": "country",
        "country_url": f"#/countries/{country}/research",
        "locator": {"document_version_id": version.id},
        "boundary": "保留书目与版本；可用内容以国别空间的权利状态为准",
    }


@router.get("/research-cases/{case_id}/library")
def library(case_id: int, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "view")
    case = db.get(ResearchCase, case_id)
    country = (case.scope or {}).get("country_iso3", "COD")
    rows = list(
        db.scalars(
            select(ProjectEvidence)
            .where(ProjectEvidence.research_case_id == case_id)
            .order_by(ProjectEvidence.id.desc())
        )
    )
    from app.models.tracking_schedule import TrackingObservation
    from app.services import field_access

    visible = []
    for row in rows:
        try:
            if row.object_type == "field_material":
                field_access.resolve_field_material_access(db, actor, row.object_id, case_id, "read")
            elif row.object_type == "tracking_observation":
                observation = db.get(TrackingObservation, row.object_id)
                if not observation:
                    continue
                field_access.enforce_run_access(db, db.get(CapabilityRun, observation.run_id), actor)
            visible.append(row)
        except field_access.FieldAccessDenied:
            continue
    rows = visible
    items = [
        {
            "id": row.id,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "decision": row.decision,
            "note": row.note,
            "snapshot": row.snapshot,
            "task_id": row.task_id,
            "reviewed_at": row.reviewed_at,
        }
        for row in rows
    ]
    seen = {(r.object_type, r.object_id) for r in rows}
    decisions = workflow.adoption_decisions(db, case_id)
    for link in db.scalars(
        select(ResearchCaseDocument).where(ResearchCaseDocument.research_case_id == case_id)
    ):
        key = ("document_version", link.document_version_id)
        if key not in seen:
            items.append(
                {
                    "id": None,
                    "object_type": key[0],
                    "object_id": key[1],
                    "decision": decisions.get(key, "accepted"),
                    "note": link.note,
                    "snapshot": _material_snapshot(db, link.document_version_id, country),
                    "legacy": True,
                }
            )
    return {
        "items": items,
        "counts": {
            status: sum(i["decision"] == status for i in items)
            for status in ["pending", "accepted", "rejected", "needs_revision"]
        },
    }


class AdoptionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    object_type: str = Field(
        pattern=r"^(document_version|event_mention|structured_observation_version|tracking_observation)$"
    )
    object_id: int = Field(gt=0)
    expected_decision: str | None = Field(
        default=None, pattern=r"^(pending|accepted|rejected|needs_revision)$"
    )


class AdoptionPayload(BaseModel):
    allow_partial: bool = False
    model_config = ConfigDict(extra="forbid")
    items: list[AdoptionItem] = Field(min_length=1, max_length=100)
    decision: str = Field(pattern=r"^(accepted|rejected|needs_revision)$")
    note: str = Field(default="", max_length=2000)


@router.post("/research-cases/{case_id}/library/reviews", dependencies=[Depends(require_reader_write_access)])
def review_library(case_id: int, payload: AdoptionPayload, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "revise")
    case = db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    available = {(r["object_type"], r["object_id"]): r for r in library(case_id, db, actor)["items"]}
    requested = []
    for item in payload.items:
        key = (item.object_type, item.object_id)
        if key not in available:
            raise HTTPException(422, "证据不在此项目候选或历史资料中")
        if item.expected_decision is not None and item.expected_decision != available[key]["decision"]:
            raise HTTPException(409, "资料审阅状态已改变，请刷新后再操作")
        if key not in [existing[0] for existing in requested]:
            requested.append((key, available[key]))
    failures = []
    if payload.decision == "accepted":
        valid = []
        for (kind, oid), item in requested:
            try:
                country = (case.scope or {}).get("country_iso3") or item["snapshot"].get("country_iso3")
                if not country:
                    raise HTTPException(409, "候选未记录国家范围，暂不能采用")
                refs = []
                if kind == "tracking_observation":
                    from app.models.tracking_schedule import TrackingObservation
                    from app.services.field_access import enforce_run_access

                    observation = db.get(TrackingObservation, oid)
                    if not observation:
                        raise HTTPException(409, "观察版本不可用")
                    enforce_run_access(db, db.get(CapabilityRun, observation.run_id), actor, "cite")
                    valid.append(((kind, oid), item))
                    continue
                if kind == "event_mention":
                    mention = db.get(EventMention, oid)
                    if not mention or mention.review_status != "confirmed":
                        raise HTTPException(409, "事件提及复核状态已改变")
                    refs = [
                        {"type": "event", "id": mention.event_id},
                        {"type": "document_version", "id": mention.document_version_id},
                    ]
                elif kind == "structured_observation_version":
                    refs = [
                        {
                            "type": "observation",
                            "id": oid,
                            "snapshot_id": item["snapshot"].get("locator", {}).get("snapshot_id"),
                        }
                    ]
                else:
                    refs = [{"type": "document_version", "id": oid}]
                reader._validated_country_references(
                    db,
                    reader.AssistantQuestionPayload(
                        question="校验项目资料采用范围",
                        context={"space": "country", "country_iso3": country},
                        references=refs,
                    ),
                )
                if kind == "document_version" and not reader._material_is_confirmed(db, oid):
                    raise HTTPException(409, "平台来源复核状态已改变，暂不能采用")
            except HTTPException as exc:
                if not payload.allow_partial:
                    raise
                reasons = {
                    "material is outside confirmed country scope": (
                        "该版本尚未通过本项目国家范围复核，请核对来源版本与国家关联。"
                    ),
                    "material is restricted": "资料访问或引用权限受限。",
                    "observation is outside permitted snapshot scope": "观测快照范围或引用权限未通过校验。",
                }
                failures.append(
                    {
                        "object_type": kind,
                        "object_id": oid,
                        "title": item["snapshot"].get("title", "资料"),
                        "reason": reasons.get(str(exc.detail), str(exc.detail)),
                    }
                )
            else:
                valid.append(((kind, oid), item))
        requested = valid
    for (kind, oid), item in requested:
        row = workflow.add_candidate(db, case_id, item.get("task_id"), kind, oid, item["snapshot"])
        row.decision, row.note, row.reviewed_by, row.reviewed_at = (
            payload.decision,
            payload.note,
            actor.id,
            datetime.now(UTC),
        )
        if payload.decision == "accepted" and kind == "document_version":
            if not reader._material_is_confirmed(db, oid):
                raise HTTPException(409, "平台来源复核状态已改变，暂不能采用")
            link = db.scalar(
                select(ResearchCaseDocument).where(
                    ResearchCaseDocument.research_case_id == case_id,
                    ResearchCaseDocument.document_version_id == oid,
                )
            )
            if not link:
                db.add(
                    ResearchCaseDocument(
                        research_case_id=case_id,
                        document_version_id=oid,
                        added_by=actor.id,
                        usage_type="background",
                        note=payload.note,
                    )
                )
        if payload.decision == "accepted" and kind == "event_mention":
            event_id = db.get(EventMention, oid).event_id
            if not db.scalar(
                select(ResearchCaseEvent.id).where(
                    ResearchCaseEvent.research_case_id == case_id, ResearchCaseEvent.event_id == event_id
                )
            ):
                db.add(
                    ResearchCaseEvent(research_case_id=case_id, event_id=event_id, usage_type="background")
                )
        record_contribution(
            db,
            research_case_id=case_id,
            user_id=actor.id,
            action_type="evidence_reviewed",
            object_type=kind,
            object_key=oid,
            details={
                "object_type": kind,
                "object_id": oid,
                "decision": payload.decision,
                "note": payload.note,
            },
        )
    db.commit()
    return {**library(case_id, db, actor), "reviewed_count": len(requested), "failures": failures}


@router.get("/project-research-frontiers")
def frontiers(
    db: DbSession,
    country_iso3: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    years: Annotated[int, Query(ge=1, le=5)] = 3,
):
    if country_iso3:
        reader._require_catalog_country(country_iso3)
    now = datetime.now(UTC)
    # Rolling years, with a valid leap-day boundary.
    try:
        start = now.replace(year=now.year - years)
    except ValueError:
        start = now.replace(year=now.year - years, day=28)
    # Aggregate once; the public search endpoint remains limited to 500 per page.
    items = reader._search_materials(
        db,
        country_iso3=country_iso3,
        channel="frontier_research",
        published_from=start,
        published_to=now,
        limit=None,
    )["items"]
    groups, items = direction_groups(items, q)
    trends = research_trends(items, years, now.date())
    trends["from"] = start.date()
    rows = []
    paper_fields = (
        "document_id",
        "document_version_id",
        "title",
        "source_name",
        "published_at",
        "published_at_precision",
        "metadata",
    )
    for group in groups:
        members = group.pop("members")
        papers = [{key: item.get(key) for key in paper_fields} for item in members[:5]]
        rows.append(
            {
                **group,
                "count": len(members),
                "papers": papers,
                "country_iso3": country_iso3,
                **frontier_relations(db, members, country_iso3),
            }
        )
    catalog = reader.get_country_data_catalog(country_iso3, db) if country_iso3 and rows else None
    for row in rows:
        theme = row["title"]
        related = row["related"]
        reason = row.pop("direction_reason")
        data = []
        if country_iso3:
            data = list(
                structured_candidates(
                    catalog,
                    [row.get("legacy_theme") or theme],
                    start,
                    now,
                )
            )[:3]
        row["candidate_data"] = [{"object_id": oid, **snap} for oid, snap in data]
        row["suggestion_id"] = (
            "direction-"
            + hashlib.sha256(f"{country_iso3 or '*'}:{row['direction_key']}".encode()).hexdigest()[:24]
        )
        row["candidate_document_version_ids"] = [p["document_version_id"] for p in row["papers"]]
        row["evidence_gaps"] = []
        if not (related["events"] or related["policies"]):
            row["evidence_gaps"].append("尚无已登记的事件关联；文献研究仍可开始")
        if not data:
            row["evidence_gaps"].append("当前筛选未匹配可引用数据，定量条件需继续核对")
        row["recommendation_reason"] = (
            reason + f"本站收录 {row['count']} 篇相关论文；"
            f"已登记 {len(related['events']) + len(related['policies'])} 项事件关联，"
            f"匹配 {len(data)} 项可引用数据候选。数据匹配只表示研究条件，不证明论文引用关系。"
        )
        dates = [str(p.get("published_at") or "") for p in row["papers"]]
        row["latest_date"] = max(dates, default="")
        row["evidence_type_count"] = 1 + bool(related["events"] or related["policies"]) + bool(data)
    rows.sort(
        key=lambda row: (
            bool(q and q.casefold() in row["title"].casefold()),
            -next((i for i, (key, _) in enumerate(DOMAINS) if key == row.get("domain_id")), len(DOMAINS)),
            row["evidence_type_count"],
            row["latest_date"],
            row["count"],
            row["title"],
        ),
        reverse=True,
    )
    return {
        "items": rows,
        "direction_count": len(rows),
        "trends": trends,
        "country_iso3": country_iso3,
        "years": years,
        "method": "country-space-recorded-research",
        "limitations": [
            "细分方向按已登记题名与研究元数据匹配，篇数可交叉；属于选题建议，不代表全球学术热点或已证实的研究空白。"
        ],
    }


class OutputDraftPayload(BaseModel):
    instruction: str = Field(default="", max_length=12000)
    model_config = ConfigDict(extra="forbid")
    plan_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=64)
    material_type: str | None = None
    references: list[dict] | None = None
    previous_target_id: int | None = None


def _usable_output_evidence(case_id: int, db, actor):
    from app.services.project_candidates import adopted_observations

    case = db.get(ResearchCase, case_id)
    scope = case.scope or {}
    countries = scope.get("country_iso3s") or ([scope["country_iso3"]] if scope.get("country_iso3") else None)
    decisions = workflow.adoption_decisions(db, case_id)
    mentions = workflow.adopted_mention_ids(db, case_id)
    _, observations = adopted_observations(db, case_id, countries)
    usable = []
    for item in library(case_id, db, actor)["items"]:
        kind, oid = item["object_type"], item["object_id"]
        if not workflow.is_adopted(decisions, kind, oid):
            continue
        if kind == "structured_observation_version":
            if oid not in observations:
                continue
            item = {**item, "snapshot": {**item["snapshot"], "locator": observations[oid]}}
        elif kind == "document_version":
            if not workflow.adopted_document(db, decisions, oid) or not reader._material_is_confirmed(
                db, oid
            ):
                continue
        elif kind == "event_mention":
            mention = db.get(EventMention, oid)
            event = db.get(ResearchEvent, mention.event_id) if mention else None
            if oid not in mentions or not event or event.review_status != "reviewed":
                continue
        else:
            continue
        usable.append(item)
    from app.services.material_evidence import enrich_evidence

    return enrich_evidence(db, case_id, usable, actor)


def _validate_legacy_output_references(output, case_id: int, db):
    """Reject stale recognized locators without claiming unknown legacy formats verified."""
    from app.services.project_candidates import adopted_observations

    case = db.get(ResearchCase, case_id)
    scope = case.scope or {}
    countries = scope.get("country_iso3s") or ([scope["country_iso3"]] if scope.get("country_iso3") else None)
    decisions = workflow.adoption_decisions(db, case_id)
    mentions = workflow.adopted_mention_ids(db, case_id)
    fields = workflow.adopted_field_material_ids(db, case_id)
    _, observations = adopted_observations(db, case_id, countries)
    events = set(
        db.scalars(
            select(EventMention.event_id)
            .join(ResearchEvent, ResearchEvent.id == EventMention.event_id)
            .where(EventMention.id.in_(mentions), ResearchEvent.review_status == "reviewed")
        )
    )
    aliases = {
        "document-version": "document_version_id",
        "event-mention": "event_mention_id",
        "event": "event_id",
        "field-material": "field_material_id",
        "observation-version": "observation_version_id",
    }
    pending = [output]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(item for item in value.values() if isinstance(item, (list, dict)))
            for plural, singular in (
                ("document_version_ids", "document_version_id"),
                ("event_mention_ids", "event_mention_id"),
                ("event_ids", "event_id"),
                ("field_material_ids", "field_material_id"),
                ("observation_version_ids", "observation_version_id"),
            ):
                if plural in value:
                    if not isinstance(value[plural], list):
                        raise HTTPException(409, "历史成果证据定位列表无效，请重新生成")
                    pending.extend(
                        {singular: oid, "snapshot_id": value.get("snapshot_id")} for oid in value[plural]
                    )
            for ref_key in ("source_ref", "source_id", "evidence_id"):
                ref = value.get(ref_key)
                if isinstance(ref, str) and ":" in ref:
                    prefix, raw_id = ref.split(":", 1)
                    if prefix in aliases:
                        if not raw_id.isdigit():
                            raise HTTPException(409, "历史成果来源引用格式无效，请重新生成")
                        pending.append(
                            {aliases[prefix]: int(raw_id), "snapshot_id": value.get("snapshot_id")}
                        )
            for key in (
                "document_version_id",
                "event_mention_id",
                "event_id",
                "field_material_id",
                "observation_version_id",
            ):
                if key not in value:
                    continue
                oid = value[key]
                if not isinstance(oid, int) or isinstance(oid, bool):
                    raise HTTPException(409, "历史成果证据定位不完整，请重新生成")
                allowed = False
                if key == "document_version_id":
                    allowed = workflow.adopted_document(db, decisions, oid) and reader._material_is_confirmed(
                        db, oid
                    )
                elif key == "event_mention_id":
                    mention = db.get(EventMention, oid)
                    event = db.get(ResearchEvent, mention.event_id) if mention else None
                    allowed = oid in mentions and event is not None and event.review_status == "reviewed"
                elif key == "event_id":
                    allowed = oid in events
                elif key == "field_material_id":
                    allowed = oid in fields
                else:
                    locator = observations.get(oid)
                    allowed = locator is not None and value.get("snapshot_id") == locator["snapshot_id"]
                if not allowed:
                    raise HTTPException(409, "历史成果引用已失效或缺少采用快照，请重新生成")


def _validate_output_citations(citations, usable):
    from app.services.material_evidence import evidence_fingerprint

    available = {(item["object_type"], item["object_id"]): item for item in usable}
    for citation in citations:
        item = available.get((citation["object_type"], citation["object_id"]))
        if (
            item is None
            or citation.get("locator", {}) != item["snapshot"].get("locator", {})
            or (
                citation.get("evidence_hash")
                and evidence_fingerprint(citation) != evidence_fingerprint(item["snapshot"])
            )
        ):
            raise HTTPException(409, "草稿引用的采用状态、来源定位或许可已改变，请重新生成")


def _generate_output(
    case_id: int, task_id: str, payload: OutputDraftPayload, db: DbSession, actor: ReaderActor
):
    reader._case_permission(db, case_id, actor, "run_skill")
    task = _task(db, case_id, task_id)
    plan = workflow.get_plan(db, task)
    if not plan or payload.plan_revision != task.revision or task.status not in {"review", "completed"}:
        raise HTTPException(409, "请先执行当前计划并审阅资料")
    workflow.validate_plan(db, task, plan)
    from app.services.research_materials import normalize_type

    kind = payload.material_type or normalize_type(plan.content.get("output_type"))
    if kind:
        from app.api.project_materials import MaterialDraftPayload, create_draft

        refs = payload.references
        if refs is None:
            refs = [
                {"object_type": r["object_type"], "object_id": r["object_id"]}
                for r in _usable_output_evidence(case_id, db, actor)
            ]
        return create_draft(
            case_id,
            MaterialDraftPayload(
                material_type=kind,
                instruction=payload.instruction,
                references=refs,
                idempotency_key=payload.idempotency_key,
                previous_target_id=payload.previous_target_id,
            ),
            db,
            actor,
        )
    existing = next((m for m in task.messages if m.get("output_request") == payload.idempotency_key), None)
    accepted = [
        item
        for item in _usable_output_evidence(case_id, db, actor)
        if item["object_type"] != "country_profile"
    ]
    if existing:
        _validate_output_citations(existing["document"]["citations"], accepted)
        return {"run_id": existing["run_id"], "document": existing["document"]}
    if not accepted:
        raise HTTPException(409, "尚无已采用资料，不能生成正式成果草稿")
    template = db.scalar(
        select(CapabilityTemplate).where(
            CapabilityTemplate.slug == "topic-digest", CapabilityTemplate.status == "active"
        )
    )
    if not template:
        raise HTTPException(409, "团队周报能力尚未配置")
    case = db.get(ResearchCase, case_id)
    config = db.scalar(
        select(CapabilityConfig)
        .where(
            CapabilityConfig.template_id == template.id,
            CapabilityConfig.research_case_id == case_id,
            CapabilityConfig.status == "active",
        )
        .order_by(CapabilityConfig.id)
    )
    if not config:
        config = CapabilityConfig(
            template_id=template.id,
            owner_id=actor.id,
            research_case_id=case_id,
            scope_type="research_case",
            name=f"项目周报 · {case_id}",
            config={"frequency": "on_demand"},
            status="active",
        )
        db.add(config)
        db.flush()
    # Reuse the registered digest executor and its evidence/claim validation.
    run = run_capability(
        db,
        config_id=config.id,
        research_case_id=case_id,
        input_overrides={"frequency": "on_demand"},
        requested_by=actor.id,
        research_progress=True,
        writing_instruction=payload.instruction,
    )
    citations = []
    for index, item in enumerate(accepted, 1):
        snap = item["snapshot"]
        source_version_id = snap.get("locator", {}).get("document_version_id")
        source_version = db.get(DocumentVersion, source_version_id) if source_version_id else None
        citations.append(
            {
                "number": index,
                "object_type": item["object_type"],
                "object_id": item["object_id"],
                "title": snap.get("title", "资料"),
                "source_name": snap.get("source_name", ""),
                "country_url": snap.get("country_url"),
                "locator": snap.get("locator", {}),
                "published_at": snap.get("published_at"),
                "published_at_precision": (
                    _published_precision(source_version)
                    if source_version
                    else snap.get("published_at_precision")
                ),
                "source_url": snap.get("source_url") or snap.get("canonical_url"),
            }
        )
    _validate_output_citations(citations, _usable_output_evidence(case_id, db, actor))
    document = {
        "title": f"{case.title} · {plan.content['output_type']} · {datetime.now(UTC).date()}",
        "type": plan.content["output_type"],
        "citations": citations,
        "task_id": task_id,
        "plan_revision": task.revision,
        "plan_id": plan.id,
        "writing_instruction": payload.instruction,
        "brief_snapshot": plan.content["brief_snapshot"],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    retrieval = plan.content.get("retrieval")
    lines = [
        f"# {document['title']}",
        "",
        "本周研究进展记录；所引文献的发表日期不等于本周发生的新事件。",
        "",
        "## 研究目标",
        case.research_question,
        "",
        "## 本轮完成的研究工作",
        f"- 已讨论并确认计划 v{task.revision}；依据人工采用的 {len(citations)} 条资料形成此稿。",
    ]
    if retrieval:
        lines += [
            f"- 库内检索词：{'、'.join(retrieval['terms'])}。",
            f"- 匹配 {retrieval['raw_count']} 条记录，去重后 {retrieval['unique_count']} 条；"
            f"其中 {retrieval['existing_count']} 条已在项目内，"
            f"新增 {retrieval['new_pending_count']} 条待选。",
        ]
    else:
        lines.append("- 历史运行未记录检索统计。")
    lines += ["", "## 文献与证据发现"]
    lines += [
        f"- [{c['number']}] {c['title']} — {c['source_name']}"
        f"（{workflow.citation_date(c['published_at'], c['published_at_precision'])}）"
        for c in citations
    ]
    agent_artifact = (run.output or {}).get("ai_digest") or {}
    prefixes = {"document_version": "document-version", "event_mention": "event-mention"}
    citation_numbers = {
        f"{prefixes.get(c['object_type'], c['object_type'])}:{c['object_id']}": c["number"] for c in citations
    }
    lines += ["", "## 可支持的认识"]
    for section in agent_artifact.get("sections", []):
        lines += ["", f"### {section['title']}"]
        for claim in section.get("claims", []):
            refs = [citation_numbers[eid] for eid in claim.get("evidence_ids", []) if eid in citation_numbers]
            lines.append(claim["text"] + " " + " ".join(f"[{number}]" for number in refs))
    if not agent_artifact.get("sections"):
        lines.append("本轮未形成可核验的 AI 综合认识；当前草稿仅保留已采用资料和研究执行记录。")
    lines += [
        "",
        "## 局限与待核实事项",
        "本稿依据项目已采用资料整理；书目信息不等同于全文结论。请核对来源并补充研究者判断。",
        *[f"- {gap}" for gap in plan.content.get("evidence_gaps", [])],
        "",
        "## 下周安排（建议，待研究者确认）",
        "- 阅读具备使用依据的原文，核对研究方法、样本范围及结论。",
        "- 对照已采用材料逐项补充证据，保留不一致或不足之处。",
        "- 根据核对结果修订研究问题，并更新研究进展周报。",
    ]
    document["markdown"] = "\n".join(lines)
    document["edit_revision"] = 1
    run.output = {**run.output, "document": document}
    run.input_snapshot = {
        **run.input_snapshot,
        "project_plan_id": plan.id,
        "project_plan_snapshot": plan.content,
        "project_evidence": [
            {"object_type": r["object_type"], "object_id": r["object_id"]} for r in accepted
        ],
    }
    # A bibliography report is a real bounded output even when synthesis finds insufficient claims.
    if run.status == "insufficient_data":
        run.status = "succeeded"
    if run.status != "succeeded":
        raise HTTPException(503, "周报能力运行未完成，资料已保留，可重试")
    workflow.append_message(
        task,
        "assistant",
        "成果草稿已生成，请预览、编辑后确认保存。",
        output_request=payload.idempotency_key,
        run_id=run.id,
        document=document,
    )
    db.commit()
    return {"run_id": run.id, "document": document}


class SaveOutputPayload(BaseModel):
    expected_revision: int | None = Field(default=None, ge=1)
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=8, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    markdown: str = Field(min_length=1, max_length=50000)


@router.post(
    "/research-cases/{case_id}/output-drafts/{run_id}/confirm",
    dependencies=[Depends(require_reader_write_access)],
)
def save_output(case_id: int, run_id: int, payload: SaveOutputPayload, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "confirm")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    run = db.get(CapabilityRun, run_id)
    if (
        not run
        or run.research_case_id != case_id
        or run.status != "succeeded"
        or "document" not in run.output
    ):
        raise HTTPException(404, "成果草稿不存在")
    reader._require_capability_run_access(db, run, actor, "confirm")
    if run.output["document"].get("material_type"):
        from app.api.project_materials import confirm_material

        return confirm_material(case_id, run, payload, db, actor)
    previous = db.scalar(
        select(CapabilityRunTarget).where(
            CapabilityRunTarget.capability_run_id == run_id,
            CapabilityRunTarget.target_type == "research_case",
            CapabilityRunTarget.target_key == str(case_id),
        )
    )
    key_owner = db.scalar(
        select(CapabilityRunTarget).where(CapabilityRunTarget.idempotency_key == payload.idempotency_key)
    )
    if key_owner is not None and (previous is None or key_owner.id != previous.id):
        raise HTTPException(409, "保存请求标识已被使用，请刷新后重新保存")
    if previous:
        saved = (previous.artifact_snapshot.get("output") or {}).get("document") or {}
        if saved.get("title") != payload.title or saved.get("markdown") != payload.markdown:
            raise HTTPException(409, "该草稿已保存，提交内容与已保存版本不同，请创建新草稿")
        _validate_output_citations(saved.get("citations", []), _usable_output_evidence(case_id, db, actor))
        return {"target_id": previous.id, "version": saved.get("version"), "idempotent_replay": True}
    doc = run.output["document"]
    if payload.expected_revision is not None and payload.expected_revision != doc.get("edit_revision", 1):
        raise HTTPException(409, "草稿已被修改，请重新读取后确认")
    task = _task(db, case_id, doc["task_id"])
    plan = db.get(ProjectPlan, doc["plan_id"])
    workflow.validate_plan(db, task, plan)
    _validate_output_citations(doc["citations"], _usable_output_evidence(case_id, db, actor))
    version = 1 + db.scalar(
        select(func.count(CapabilityRunTarget.id)).where(
            CapabilityRunTarget.target_type == "research_case", CapabilityRunTarget.target_key == str(case_id)
        )
    )
    document = {
        **doc,
        "title": payload.title,
        "markdown": payload.markdown,
        "version": version,
        "editorial_status": "researcher_confirmed",
        "edited_by": actor.id,
    }
    target = CapabilityRunTarget(
        capability_run_id=run.id,
        target_type="research_case",
        target_key=str(case_id),
        idempotency_key=payload.idempotency_key,
        confirmed_by=actor.id,
        artifact_snapshot={
            "output": {**run.output, "document": document},
            "input_snapshot": run.input_snapshot,
            "confirmed_status": run.status,
            "revision_id": None,
        },
    )
    run.review_status = "approved"
    task.status = "completed"
    db.add(target)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "并发保存请求发生冲突，请重新读取成果后重试") from exc
    workflow.append_message(
        task, "assistant", f"已保存成果《{payload.title}》，可随时从研究成果打开。", output_id=target.id
    )
    db.commit()
    return {"target_id": target.id, "version": version}


class EditOutputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    markdown: str = Field(min_length=1, max_length=50000)


def _output_run(db, case_id, run_id):
    run = db.scalar(
        select(CapabilityRun)
        .where(CapabilityRun.id == run_id, CapabilityRun.research_case_id == case_id)
        .execution_options(populate_existing=True)
    )
    if not run or "document" not in (run.output or {}):
        raise HTTPException(404, "成果草稿不存在")
    return run


@router.get("/research-cases/{case_id}/output-drafts/{run_id}")
def read_output_draft(case_id: int, run_id: int, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "view")
    run = _output_run(db, case_id, run_id)
    reader._require_capability_run_access(db, run, actor, "view")
    _validate_output_citations(
        run.output["document"]["citations"], _usable_output_evidence(case_id, db, actor)
    )
    target = db.scalar(
        select(CapabilityRunTarget).where(
            CapabilityRunTarget.capability_run_id == run_id,
            CapabilityRunTarget.target_type == "research_case",
            CapabilityRunTarget.target_key == str(case_id),
        )
    )
    return {"run_id": run_id, "document": run.output["document"], "target_id": target.id if target else None}


@router.patch(
    "/research-cases/{case_id}/output-drafts/{run_id}", dependencies=[Depends(require_reader_write_access)]
)
def edit_output_draft(
    case_id: int, run_id: int, payload: EditOutputPayload, db: DbSession, actor: ReaderActor
):
    reader._case_permission(db, case_id, actor, "edit")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    run = _output_run(db, case_id, run_id)
    reader._require_capability_run_access(db, run, actor, "view")
    if db.scalar(select(CapabilityRunTarget.id).where(CapabilityRunTarget.capability_run_id == run_id)):
        raise HTTPException(409, "正式版本不可覆盖，请另建草稿")
    doc = run.output["document"]
    if payload.expected_revision != doc.get("edit_revision", 1):
        if doc["title"] == payload.title and doc["markdown"] == payload.markdown:
            return {"run_id": run_id, "document": doc}
        raise HTTPException(409, "草稿已被修改，本地输入已保留，请重新读取")
    _validate_output_citations(doc["citations"], _usable_output_evidence(case_id, db, actor))
    doc = {
        **doc,
        "title": payload.title,
        "markdown": payload.markdown,
        "edit_revision": doc.get("edit_revision", 1) + 1,
        "edited_by": actor.id,
    }
    run.output = {**run.output, "document": doc}
    db.commit()
    return {"run_id": run_id, "document": doc}


class CopyOutputPayload(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)


@router.post(
    "/research-cases/{case_id}/outputs/{target_id}/draft", dependencies=[Depends(require_reader_write_access)]
)
def copy_output_draft(
    case_id: int, target_id: int, payload: CopyOutputPayload, db: DbSession, actor: ReaderActor
):
    reader._case_permission(db, case_id, actor, "edit")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    target = db.get(CapabilityRunTarget, target_id)
    source = _output_run(db, case_id, target.capability_run_id) if target else None
    if source is None:
        raise HTTPException(404, "成果不存在")
    reader._require_capability_run_access(db, source, actor, "view")
    doc = (target.artifact_snapshot.get("output") or {}).get("document")
    if not doc:
        raise HTTPException(409, "旧格式成果请按当前计划重新生成")
    _validate_output_citations(doc["citations"], _usable_output_evidence(case_id, db, actor))
    for run in db.scalars(select(CapabilityRun).where(CapabilityRun.research_case_id == case_id)):
        if (run.input_snapshot or {}).get("copy_request") == payload.idempotency_key:
            if run.input_snapshot.get("source_target_id") != target_id:
                raise HTTPException(409, "请求标识已用于其他成果")
            return {"run_id": run.id, "document": run.output["document"]}
    doc = {k: v for k, v in doc.items() if k not in {"version", "editorial_status"}}
    doc = {**doc, "edit_revision": 1}
    run = CapabilityRun(
        config_id=source.config_id,
        config_revision_id=source.config_revision_id,
        research_case_id=case_id,
        requested_by=actor.id,
        status="succeeded",
        input_snapshot={
            **source.input_snapshot,
            "copy_request": payload.idempotency_key,
            "source_target_id": target_id,
        },
        output={**source.output, "document": doc},
    )
    db.add(run)
    db.commit()
    return {"run_id": run.id, "document": doc}


def _generation_active(record):
    if record.get("generation_status") != "running":
        return False
    if record.get("generation_host") == socket.gethostname() and record.get("generation_pid"):
        try:
            os.kill(record["generation_pid"], 0)
        except ProcessLookupError:
            return False
    return True


@router.get("/research-cases/{case_id}/tasks/{task_id}/output-requests/{request_id}")
def output_request_status(case_id: int, task_id: str, request_id: str, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "view")
    task = _task(db, case_id, task_id)
    completed = next((m for m in task.messages if m.get("output_request") == request_id), None)
    if completed:
        return {"status": "completed", **read_output_draft(case_id, completed["run_id"], db, actor)}
    record = next((m for m in reversed(task.messages) if m.get("generation_request") == request_id), None)
    if not record:
        raise HTTPException(404, "生成请求尚未登记")
    if record["generation_status"] == "running" and not _generation_active(record):
        return {"status": "failed", "error": "生成进程已中断，资料保留，可重新生成"}
    return {"status": record["generation_status"], "error": record.get("error")}


@router.post(
    "/research-cases/{case_id}/tasks/{task_id}/output-draft",
    dependencies=[Depends(require_reader_write_access)],
)
def draft_output(case_id: int, task_id: str, payload: OutputDraftPayload, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "run_skill")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    task = _task(db, case_id, task_id)
    from app.services.research_materials import normalize_type

    plan = workflow.get_plan(db, task)
    if payload.instruction and not any(
        m.get("writing_request") == payload.idempotency_key for m in task.messages
    ):
        workflow.append_message(task, "user", payload.instruction, writing_request=payload.idempotency_key)
        db.commit()
    if payload.material_type or (plan and normalize_type(plan.content.get("output_type"))):
        result = _generate_output(case_id, task_id, payload, db, actor)
        if not any(m.get("writing_result") == payload.idempotency_key for m in task.messages):
            workflow.append_message(
                task,
                "assistant",
                "成果草稿已放入右侧成果栏，可继续审阅与编辑。",
                writing_result=payload.idempotency_key,
            )
            db.commit()
        return result
    records = [m for m in task.messages if m.get("generation_request") == payload.idempotency_key]
    if records:
        if records[-1].get("plan_revision") != payload.plan_revision:
            raise HTTPException(409, "生成请求对应另一版计划")
        return output_request_status(case_id, task_id, payload.idempotency_key, db, actor)
    finished = {m.get("output_request") for m in task.messages if m.get("output_request")}
    latest_requests = {m["generation_request"]: m for m in task.messages if m.get("generation_request")}
    if any(key not in finished and _generation_active(record) for key, record in latest_requests.items()):
        raise HTTPException(409, "当前对话已有成果生成请求，请先查询已有请求状态")
    workflow.append_message(
        task,
        "assistant",
        "正在生成成果草稿。",
        generation_request=payload.idempotency_key,
        generation_status="running",
        generation_host=socket.gethostname(),
        generation_pid=os.getpid(),
        plan_revision=payload.plan_revision,
    )
    db.commit()
    try:
        result = _generate_output(case_id, task_id, payload, db, actor)
        return {"status": "completed", **result}
    except Exception as exc:
        db.rollback()
        task = _task(db, case_id, task_id)
        detail = str(exc.detail) if isinstance(exc, HTTPException) else "成果生成失败，请保留资料并重新生成"
        workflow.append_message(
            task,
            "assistant",
            detail,
            generation_request=payload.idempotency_key,
            generation_status="failed",
            plan_revision=payload.plan_revision,
            error=detail,
        )
        db.commit()
        raise


@router.delete(
    "/research-cases/{case_id}/tasks/{task_id}", dependencies=[Depends(require_reader_write_access)]
)
def delete_conversation(case_id: int, task_id: str, db: DbSession, actor: ReaderActor):
    reader._case_permission(db, case_id, actor, "edit")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    if task_id in workflow.deleted_conversation_ids(db, case_id):
        return {"deleted": True, "id": task_id}
    task = reader.get_research_case_task(case_id, task_id, db, actor)
    if task.get("kind") != "conversation":
        raise HTTPException(422, "只能删除研究对话")
    managed = db.get(ProjectTask, task_id)
    running = db.scalar(
        select(AgentRun.id).where(
            AgentRun.conversation_id == task_id,
            AgentRun.scope_type == "topic",
            AgentRun.scope_key == str(case_id),
            AgentRun.status.in_(["queued", "running"]),
        )
    )
    if running or (managed and (managed.status == "running" or workflow.plan_in_progress(managed))):
        raise HTTPException(409, "对话正在运行，请先停止后再删除")
    record_contribution(
        db,
        research_case_id=case_id,
        user_id=actor.id,
        action_type="conversation_deleted",
        object_type="project_conversation",
        object_key=task_id,
        details={"title": task.get("title", "研究对话"), "retained": "evidence_and_outputs"},
    )
    db.commit()
    return {"deleted": True, "id": task_id}
