"""Run an explicitly selected Skill as a persistent turn in a project conversation."""

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api import research as reader
from app.api.skill_workflows import material_access
from app.models import (
    AgentRun,
    CapabilityConfig,
    CapabilityRun,
    CapabilityTemplate,
    ProjectTask,
    ResearchCase,
)
from app.services import project_research as workflow
from app.services.research_capabilities import run_capability
from app.services.skill_execution_control import Control, execution_scope, request_stop

router = APIRouter(
    prefix="/api/v1/reader/skill-workflows/cases",
    tags=["project-skill-turns"],
    dependencies=[Depends(reader.require_internal_reader_access)],
)


class SkillTurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=8, max_length=36)
    request_id: str = Field(min_length=8, max_length=64)
    config_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=160)


def _turn(task, request_id):
    return next(
        (m["skill_turn"] for m in task.messages if m.get("skill_turn", {}).get("request_id") == request_id),
        None,
    )


def _update(task, request_id, **values):
    task.messages = [
        {**m, "skill_turn": {**m["skill_turn"], **values}}
        if m.get("skill_turn", {}).get("request_id") == request_id
        else m
        for m in task.messages
    ]
    task.updated_at = datetime.now(UTC)


ACTIVE = {"queued", "running", "pausing"}
LEASE_SECONDS = 30


def _locked_task(db, task_id):
    return db.scalar(
        select(ProjectTask)
        .where(ProjectTask.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def recover_interrupted(db, task):
    """A missing heartbeat is interruption, never evidence that computation is ongoing."""
    now = datetime.now(UTC)
    stale = []
    for message in task.messages:
        turn = message.get("skill_turn", {})
        if turn.get("status") not in ACTIVE:
            continue
        at = turn.get("heartbeat_at") or turn.get("started_at") or turn["events"][0]["at"]
        # Legacy turns did not have a lease; allow their full configured processing budget.
        from app.core.config import get_settings

        grace = (
            LEASE_SECONDS
            if turn.get("heartbeat_at")
            else max(180, get_settings().agent_timeout_seconds * max(1, turn.get("material_count", 1)) + 60)
        )
        if datetime.fromisoformat(at) < now - timedelta(seconds=grace):
            stale.append(turn["request_id"])
    if not stale:
        return task
    task = _locked_task(db, task.id)
    for request_id in stale:
        turn = _turn(task, request_id)
        if turn["status"] not in ACTIVE:
            continue
        # Recheck after acquiring the lock; a live worker may have just renewed its lease.
        at = turn.get("heartbeat_at") or turn.get("started_at") or turn["events"][0]["at"]
        grace = (
            LEASE_SECONDS
            if turn.get("heartbeat_at")
            else max(180, get_settings().agent_timeout_seconds * max(1, turn.get("material_count", 1)) + 60)
        )
        if datetime.fromisoformat(at) >= now - timedelta(seconds=grace):
            continue
        error = "处理进程已中断（服务重启或运行失联）。输入与补充内容已保存，可重新处理。"
        run = db.get(CapabilityRun, turn.get("run_id")) if turn.get("run_id") else None
        # A result may have committed immediately before the worker disappeared.
        status = run.status if run and run.status != "running" else "failed"
        if run and run.status == "running":
            run.status, run.error_code, run.error_message, run.finished_at = (
                "failed",
                "worker_interrupted",
                error,
                now,
            )
        _update(
            task,
            request_id,
            status=status,
            error=None
            if status == "succeeded"
            else (run.error_message if run and run.error_message else error),
            finished_at=now.isoformat(),
            events=[
                *turn["events"],
                {
                    "at": now.isoformat(),
                    "label": "已恢复保存结果" if status == "succeeded" else "处理进程中断，输入已保留",
                },
            ],
        )
        task.status = "review" if status == "succeeded" else "failed"
    db.commit()
    return task


def _heartbeat(sessions, task_id, request_id, control, done):
    while not done.wait(2):
        try:
            with sessions() as db:
                task = _locked_task(db, task_id)
                turn = _turn(task, request_id) if task else None
                if not turn or turn["status"] not in ACTIVE:
                    control.stop("任务已结束或失去执行权")
                    return
                if turn["status"] == "pausing":
                    control.stop()
                _update(
                    task,
                    request_id,
                    heartbeat_at=datetime.now(UTC).isoformat(),
                    events=[*turn["events"], *control.take_progress()][-200:],
                )
                db.commit()
        except Exception:
            control.stop("无法续报任务状态，已停止本轮处理")
            return


def _queue_followup(task, previous, actor_id, request_id=None):
    notes = [m for m in task.messages if m.get("skill_note", {}).get("status") == "queued"]
    config = dict(previous["frozen_config"])
    if notes:
        config["instruction"] = (
            str(config.get("instruction", "")) + "\n研究者补充要求：\n" + "\n".join(m["text"] for m in notes)
        )
    request_id = request_id or str(uuid4())
    now = datetime.now(UTC).isoformat()
    task.messages = [
        {**m, "skill_note": {**m["skill_note"], "status": "applied", "applied_to": request_id}}
        if m in notes
        else m
        for m in task.messages
    ]
    workflow.append_message(
        task,
        "assistant",
        "继续处理",
        skill_turn={
            "request_id": request_id,
            "kind": previous["kind"],
            "title": previous["title"],
            "config_id": previous["config_id"],
            "material_count": previous["material_count"],
            "status": "queued",
            "heartbeat_at": now,
            "frozen_config": config,
            "actor_id": actor_id,
            "events": [
                {"at": now, "label": "补充要求已加入，准备重新处理" if notes else "保留原输入，准备重新处理"}
            ],
        },
    )
    task.status = "running"
    return request_id, config


def execute_turn(sessions, task_id, request_id, config_id, frozen_config, actor_id):
    while request_id:
        control, done = Control(), threading.Event()

        pulse = threading.Thread(
            target=_heartbeat, args=(sessions, task_id, request_id, control, done), daemon=True
        )
        next_request = None
        with execution_scope((task_id, request_id), control), sessions() as db:
            task = db.get(ProjectTask, task_id)
            if not task or _turn(task, request_id)["status"] != "queued":
                return
            pulse.start()
            try:

                def started(run, request_id=request_id, control=control):
                    current = _locked_task(db, task_id)
                    turn = _turn(current, request_id)
                    if turn["status"] == "pausing":
                        control.stop()
                    at = datetime.now(UTC).isoformat()
                    _update(
                        current,
                        request_id,
                        status=turn["status"] if turn["status"] == "pausing" else "running",
                        run_id=run.id,
                        started_at=at,
                        heartbeat_at=at,
                        events=[*turn["events"], {"label": "输入已检查，正在调用 Skill 处理", "at": at}],
                    )
                    db.commit()

                run = run_capability(
                    db,
                    config_id=config_id,
                    research_case_id=task.research_case_id,
                    requested_by=actor_id,
                    input_overrides=frozen_config,
                    workflow_started=started,
                )
                current = _locked_task(db, task_id)
                turn = _turn(current, request_id)
                # A recovered/terminal turn must never be overwritten by a late worker.
                if turn["status"] not in ACTIVE:
                    return
                paused = turn["status"] == "pausing" and run.status != "succeeded"
                at = datetime.now(UTC).isoformat()
                state = "paused" if paused else run.status
                error = "已暂停；输入和补充内容保留，继续时重新处理。" if paused else run.error_message
                if paused:
                    run.error_code, run.error_message = "user_paused", error
                _update(
                    current,
                    request_id,
                    run_id=run.id,
                    status=state,
                    finished_at=at,
                    error=error,
                    events=[
                        *turn["events"],
                        *control.take_progress(),
                        {
                            "at": at,
                            "label": "已暂停处理"
                            if paused
                            else "候选结果已保存，请审阅"
                            if state == "succeeded"
                            else "处理未完成，输入已保留",
                        },
                    ],
                )
                current.status = "cancelled" if paused else "review" if state == "succeeded" else "failed"
                if (
                    state == "succeeded"
                    and turn["status"] != "pausing"
                    and turn["kind"] == "s05"
                    and any(m.get("skill_note", {}).get("status") == "queued" for m in current.messages)
                ):
                    next_request, frozen_config = _queue_followup(current, turn, actor_id)
                db.commit()
            except Exception:
                db.rollback()
                current = _locked_task(db, task_id)
                turn = _turn(current, request_id) if current else None
                if turn and turn["status"] in ACTIVE:
                    paused = turn["status"] == "pausing"
                    at = datetime.now(UTC).isoformat()
                    _update(
                        current,
                        request_id,
                        status="paused" if paused else "failed",
                        finished_at=at,
                        error="已暂停；输入已保留。"
                        if paused
                        else "本轮处理未完成，输入已保留，可重新处理。",
                        events=[
                            *turn["events"],
                            *control.take_progress(),
                            {"at": at, "label": "已暂停" if paused else "处理异常，输入已保留"},
                        ],
                    )
                    current.status = "cancelled" if paused else "failed"
                    db.commit()
            finally:
                done.set()
                pulse.join(timeout=3)
        request_id = next_request


@router.post("/{case_id}/turns", status_code=202, dependencies=[Depends(reader.require_reader_write_access)])
def start_skill_turn(
    case_id: int,
    payload: SkillTurnInput,
    background: BackgroundTasks,
    db: reader.DbSession,
    actor: reader.ReaderActor,
):
    reader._case_permission(db, case_id, actor, "run_skill")
    db.scalar(select(ResearchCase).where(ResearchCase.id == case_id).with_for_update())
    if payload.conversation_id in workflow.deleted_conversation_ids(db, case_id):
        raise HTTPException(404, "对话已删除，请新建研究")
    config = db.get(CapabilityConfig, payload.config_id)
    template = db.get(CapabilityTemplate, config.template_id) if config else None
    if (
        not config
        or config.research_case_id != case_id
        or not reader._capability_config_visible(db, config, actor, "run_skill")
    ):
        raise HTTPException(403, "Skill 配置不属于当前项目或无权使用")
    if (
        config.config.get("workflow_version") != 1
        or not template
        or template.catalog_key not in {"research-s03", "research-s05"}
        or template.validation_status != "verified"
        or template.status != "active"
        or config.status not in {"active", "published"}
    ):
        raise HTTPException(409, "此 Skill 尚未开放会话执行")
    for material_id in config.config.get("material_ids", []):
        material_access(db, material_id, actor, "ai", case_id)
    if template.catalog_key == "research-s05" and not config.config.get("material_ids"):
        raise HTTPException(422, "请选择或提交访谈材料")
    task = db.scalar(select(ProjectTask).where(ProjectTask.id == payload.conversation_id).with_for_update())
    fingerprint = hashlib.sha256(
        json.dumps({**payload.model_dump(), "config": config.config}, sort_keys=True).encode()
    ).hexdigest()
    if task:
        if task.research_case_id != case_id:
            raise HTTPException(409, "会话属于其他项目")
        task = recover_interrupted(db, task)
        existing = _turn(task, payload.request_id)
        if existing:
            if existing.get("fingerprint") != fingerprint:
                raise HTTPException(409, "同一请求标识不能用于不同输入")
            db.commit()
            return workflow.task_payload(db, task)
        if task.status == "running" or workflow.plan_in_progress(task):
            raise HTTPException(409, "此会话仍在处理上一轮，请稍后再试")
    else:
        legacy = list(db.scalars(select(AgentRun).where(AgentRun.conversation_id == payload.conversation_id)))
        if any(r.scope_type != "topic" or r.scope_key != str(case_id) for r in legacy):
            raise HTTPException(409, "会话属于其他研究范围")
        task = ProjectTask(
            id=payload.conversation_id,
            research_case_id=case_id,
            created_by=actor.id,
            title=workflow.short_title(legacy[0].question if legacy else payload.title, "研究对话"),
            messages=[],
            revision=0,
        )
        db.add(task)
    active_chat = db.scalar(
        select(AgentRun.id).where(AgentRun.conversation_id == task.id, AgentRun.status == "running")
    )
    if active_chat:
        raise HTTPException(409, "此会话仍在回复上一轮，请稍后再试")
    kind = template.catalog_key.removeprefix("research-")
    name = template.name
    workflow.append_message(task, "user", f"使用 {name}：{payload.title}", skill_request=payload.request_id)
    workflow.append_message(
        task,
        "assistant",
        name,
        skill_turn={
            "request_id": payload.request_id,
            "fingerprint": fingerprint,
            "kind": kind,
            "title": payload.title,
            "config_id": config.id,
            "material_count": len(config.config.get("material_ids", [])),
            "status": "queued",
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "frozen_config": dict(config.config),
            "actor_id": actor.id,
            "events": [{"label": "请求与材料选择已保存", "at": datetime.now(UTC).isoformat()}],
        },
    )
    task.status = "running"
    task.error = None
    db.commit()
    background.add_task(
        execute_turn,
        sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False),
        task.id,
        payload.request_id,
        config.id,
        dict(config.config),
        actor.id,
    )
    return workflow.task_payload(db, task)


class SupplementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=4000)


class ResumeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=64)


def _editable_task(db, case_id, task_id, actor):
    reader._case_permission(db, case_id, actor, "run_skill")
    if task_id in workflow.deleted_conversation_ids(db, case_id):
        raise HTTPException(404, "对话已删除")
    task = _locked_task(db, task_id)
    if not task or task.research_case_id != case_id:
        raise HTTPException(404, "对话不存在")
    recover_interrupted(db, task)
    return _locked_task(db, task_id)


@router.post("/{case_id}/tasks/{task_id}/pause", dependencies=[Depends(reader.require_reader_write_access)])
def pause_skill(case_id: int, task_id: str, db: reader.DbSession, actor: reader.ReaderActor):
    task = _editable_task(db, case_id, task_id, actor)
    turn = next(
        (m["skill_turn"] for m in reversed(task.messages) if m.get("skill_turn", {}).get("status") in ACTIVE),
        None,
    )
    if turn:
        at = datetime.now(UTC).isoformat()
        queued = turn["status"] == "queued"
        _update(
            task,
            turn["request_id"],
            status="paused" if queued else "pausing",
            error="已暂停；输入已保留。" if queued else None,
            events=[
                *turn["events"],
                {"at": at, "label": "已暂停，尚未调用模型" if queued else "正在停止当前处理"},
            ],
        )
        if queued:
            task.status = "cancelled"
        db.commit()
        request_stop((task_id, turn["request_id"]))
    return workflow.task_payload(db, task)


@router.post(
    "/{case_id}/tasks/{task_id}/messages", dependencies=[Depends(reader.require_reader_write_access)]
)
def supplement_skill(
    case_id: int, task_id: str, payload: SupplementInput, db: reader.DbSession, actor: reader.ReaderActor
):
    task = _editable_task(db, case_id, task_id, actor)
    existing = next(
        (m for m in task.messages if m.get("skill_note", {}).get("request_id") == payload.request_id), None
    )
    if existing:
        if existing["text"] != payload.text.strip():
            raise HTTPException(409, "同一请求标识不能用于不同内容")
        return workflow.task_payload(db, task)
    turn = next((m["skill_turn"] for m in reversed(task.messages) if m.get("skill_turn")), None)
    if not turn or turn["status"] not in ACTIVE | {"paused", "failed"}:
        raise HTTPException(409, "本轮已结束，请作为新的会话消息发送")
    queued = [m for m in task.messages if m.get("skill_note", {}).get("status") == "queued"]
    if len(queued) >= 10 or sum(len(m["text"]) for m in queued) + len(payload.text) > 12000:
        raise HTTPException(422, "待处理补充较多，请先处理已有内容")
    if not payload.text.strip():
        raise HTTPException(422, "补充内容不能为空")
    workflow.append_message(
        task,
        "user",
        payload.text.strip(),
        skill_note={
            "request_id": payload.request_id,
            "status": "queued" if turn["kind"] == "s05" else "recorded",
            "actor_id": actor.id,
        },
    )
    db.commit()
    return workflow.task_payload(db, task)


@router.post(
    "/{case_id}/tasks/{task_id}/resume",
    status_code=202,
    dependencies=[Depends(reader.require_reader_write_access)],
)
def resume_skill(
    case_id: int,
    task_id: str,
    payload: ResumeInput,
    background: BackgroundTasks,
    db: reader.DbSession,
    actor: reader.ReaderActor,
):
    task = _editable_task(db, case_id, task_id, actor)
    if _turn(task, payload.request_id):
        return workflow.task_payload(db, task)
    previous = next((m["skill_turn"] for m in reversed(task.messages) if m.get("skill_turn")), None)
    if not previous or previous["status"] not in {"paused", "failed"}:
        raise HTTPException(409, "当前任务尚未暂停或已经完成")
    config = db.get(CapabilityConfig, previous["config_id"])
    template = db.get(CapabilityTemplate, config.template_id) if config else None
    if (
        not config
        or config.research_case_id != case_id
        or not reader._capability_config_visible(db, config, actor, "run_skill")
        or not template
        or template.validation_status != "verified"
        or template.status != "active"
        or config.status not in {"active", "published"}
    ):
        raise HTTPException(403, "无权继续此 Skill 或能力尚未验证")
    if not previous.get("frozen_config"):
        run = db.get(CapabilityRun, previous.get("run_id"))
        previous = {
            **previous,
            "frozen_config": dict(run.input_snapshot.get("config", config.config))
            if run
            else dict(config.config),
        }
    for material_id in previous["frozen_config"].get("material_ids", []):
        material_access(db, material_id, actor, "ai", case_id)
    request_id, frozen = _queue_followup(task, previous, actor.id, payload.request_id)
    db.commit()
    background.add_task(
        execute_turn,
        sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False),
        task.id,
        request_id,
        config.id,
        frozen,
        actor.id,
    )
    return workflow.task_payload(db, task)
