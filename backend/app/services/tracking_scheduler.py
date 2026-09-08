"""Local schedules use database occurrences as a durable outbox for Celery."""

import copy
import secrets
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import CapabilityConfig, CapabilityConfigRevision, CapabilityRun, CapabilityTemplate, User
from app.models.tracking_schedule import (
    TrackingDelivery,
    TrackingOccurrence,
    TrackingSchedule,
)
from app.services import field_access, tracking_workflow, workflow_documents
from app.services.community_auth import utc
from app.services.reader_access import require_case_permission

MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=20)


def planned_times(start_date, end_date, interval_days, local_time, timezone):
    zone = ZoneInfo(timezone)
    clock = time.fromisoformat(local_time)
    if not 1 <= interval_days <= 365 or end_date < start_date or (end_date - start_date).days > 366:
        raise ValueError("追踪范围最多 366 天，执行间隔为 1 至 365 天")
    dates = []
    day = start_date
    while day <= end_date:
        dates.append(datetime.combine(day, clock, zone).astimezone(UTC))
        day += timedelta(days=interval_days)
    return dates


def schedule_end(schedule):
    return datetime.combine(schedule.end_date, time.max, ZoneInfo(schedule.timezone)).astimezone(UTC)


def eligible(db, config, actor_id):
    from app.models.field_community import ReaderAccount
    from app.services.community_auth import accounts_initialized

    actor = db.scalar(select(User).where(User.id == actor_id).execution_options(populate_existing=True))
    if not config or not actor or actor.status != "active" or config.status not in {"active", "published"}:
        raise ValueError("配置或执行账号已停用")
    if accounts_initialized(db) and not db.get(ReaderAccount, actor.id):
        raise ValueError("执行账号尚未完成邀请注册")
    require_case_permission(db, config.research_case_id, actor, "run_skill")
    template = db.get(CapabilityTemplate, config.template_id)
    if not template or template.catalog_key != "research-s03" or config.config.get("workflow_version") != 1:
        raise ValueError("仅新 S03 工作流可启动专题追踪")
    for mid in config.config.get("material_ids", []):
        field_access.resolve_field_material_access(db, actor, mid, config.research_case_id, "read")
    return actor


def start(db, config, actor, values, now=None):
    now = now or datetime.now(UTC)
    db.scalar(select(CapabilityConfig).where(CapabilityConfig.id == config.id).with_for_update())
    eligible(db, config, actor.id)
    existing = db.scalar(select(TrackingSchedule).where(TrackingSchedule.config_id == config.id))
    if existing:
        raise ValueError("该配置已有追踪计划，请查看当前计划或创建新配置")
    slots = planned_times(
        **{k: values[k] for k in ("start_date", "end_date", "interval_days", "local_time", "timezone")}
    )
    if values["end_date"] < now.astimezone(ZoneInfo(values["timezone"])).date():
        raise ValueError("截止日期已过去")
    future = [s for s in slots if s > now]
    schedule = TrackingSchedule(
        config_id=config.id,
        created_by=actor.id,
        **values,
        status="active",
        started_at=now,
        next_run_at=future[0] if future else None,
    )
    db.add(schedule)
    db.flush()
    occurrence = TrackingOccurrence(
        schedule_id=schedule.id,
        occurrence_key="baseline",
        kind="baseline",
        scheduled_at=now,
        covered_slots=[],
        available_at=now,
        status="queued",
    )
    db.add(occurrence)
    db.flush()
    return schedule, occurrence


def enqueue_due(db, now=None):
    now = now or datetime.now(UTC)
    schedules = list(
        db.scalars(
            select(TrackingSchedule)
            .where(TrackingSchedule.status == "active")
            .with_for_update(skip_locked=True)
        )
    )
    for schedule in schedules:
        slots = planned_times(
            schedule.start_date,
            schedule.end_date,
            schedule.interval_days,
            schedule.local_time,
            schedule.timezone,
        )
        due = [s for s in slots if schedule.next_run_at and utc(schedule.next_run_at) <= s <= now]
        if due:
            latest = due[-1]
            key = latest.isoformat()
            if not db.scalar(
                select(TrackingOccurrence.id).where(
                    TrackingOccurrence.schedule_id == schedule.id, TrackingOccurrence.occurrence_key == key
                )
            ):
                db.add(
                    TrackingOccurrence(
                        schedule_id=schedule.id,
                        occurrence_key=key,
                        kind="catchup" if len(due) > 1 or now - latest > timedelta(minutes=5) else "periodic",
                        scheduled_at=latest,
                        covered_slots=[d.isoformat() for d in due],
                        available_at=now,
                    )
                )
            schedule.next_run_at = next((s for s in slots if s > latest), None)
        db.flush()
        unfinished = db.scalar(
            select(TrackingOccurrence.id)
            .where(
                TrackingOccurrence.schedule_id == schedule.id,
                TrackingOccurrence.status.in_(["queued", "running"]),
            )
            .limit(1)
        )
        if now > schedule_end(schedule) and not unfinished:
            if not db.scalar(
                select(TrackingOccurrence.id).where(
                    TrackingOccurrence.schedule_id == schedule.id,
                    TrackingOccurrence.occurrence_key == "summary",
                )
            ):
                db.add(
                    TrackingOccurrence(
                        schedule_id=schedule.id,
                        occurrence_key="summary",
                        kind="summary",
                        scheduled_at=schedule_end(schedule),
                        available_at=now,
                    )
                )
    db.flush()
    # Expired worker leases can be retried, including after a machine suspension.
    stale = db.scalars(
        select(TrackingOccurrence)
        .where(TrackingOccurrence.status == "running", TrackingOccurrence.lease_expires_at < now)
        .with_for_update(skip_locked=True)
    )
    for occurrence in stale:
        occurrence.status = "queued" if occurrence.attempts < MAX_ATTEMPTS else "failed"
        occurrence.lease_token, occurrence.lease_expires_at = None, None
        occurrence.errors = [*occurrence.errors, {"at": now.isoformat(), "reason": "worker_lease_expired"}]
        occurrence.available_at = now
    db.flush()
    return list(
        db.scalars(
            select(TrackingOccurrence.id)
            .join(TrackingSchedule)
            .where(
                TrackingSchedule.status == "active",
                TrackingOccurrence.status == "queued",
                TrackingOccurrence.available_at <= now,
            )
            .order_by(TrackingOccurrence.id)
            .limit(100)
        )
    )


def claim(db, occurrence_id, now=None):
    now = now or datetime.now(UTC)
    occurrence = db.scalar(
        select(TrackingOccurrence)
        .where(TrackingOccurrence.id == occurrence_id)
        .with_for_update(skip_locked=True)
    )
    if not occurrence or occurrence.status != "queued" or utc(occurrence.available_at) > now:
        return None
    schedule = db.get(TrackingSchedule, occurrence.schedule_id)
    if schedule.status != "active":
        return None
    # Serialize all occurrences of this schedule so comparison follows observation order.
    db.scalar(select(TrackingSchedule).where(TrackingSchedule.id == schedule.id).with_for_update())
    if db.scalar(
        select(TrackingOccurrence.id)
        .where(
            TrackingOccurrence.schedule_id == schedule.id,
            TrackingOccurrence.status == "running",
            TrackingOccurrence.id != occurrence.id,
        )
        .limit(1)
    ):
        return None
    if db.scalar(
        select(TrackingOccurrence.id)
        .where(
            TrackingOccurrence.schedule_id == schedule.id,
            TrackingOccurrence.id < occurrence.id,
            TrackingOccurrence.status == "queued",
        )
        .limit(1)
    ):
        return None
    occurrence.status, occurrence.lease_token = "running", secrets.token_hex(24)
    occurrence.lease_expires_at, occurrence.started_at = now + LEASE, now
    occurrence.attempts += 1
    db.flush()
    return occurrence.lease_token


def aggregate_summary(db, schedule, run):
    latest = {}
    failures = []
    for previous in db.scalars(
        select(TrackingOccurrence)
        .where(TrackingOccurrence.schedule_id == schedule.id)
        .order_by(TrackingOccurrence.id)
    ):
        if previous.kind == "summary":
            continue
        if previous.status == "failed":
            failures.append(previous.id)
        if not previous.run_id:
            continue
        source = db.get(CapabilityRun, previous.run_id)
        for observation in (source.output or {}).get("observations", []):
            if observation["snapshot"].get("access_status") == "accessible":
                latest[observation["item_id"]] = copy.deepcopy(observation)
    observations = list(latest.values())
    for item in observations:
        item["observation"] = "new"
    return {
        "type": "policy_tracking_workflow",
        "observations": observations,
        "counts": {"new": len(observations), "updated": 0, "unchanged": 0, "excluded": 0},
        "period_summary": True,
        "failed_occurrences": failures,
        "source_failures": len(failures),
        "meaningful_changes": len(observations),
        "no_updates": not observations,
        "result_status": "candidate",
        "period": {"start": str(schedule.start_date), "end": str(schedule.end_date)},
    }


def execute_occurrence(factory, occurrence_id, token, *, client=None, now=None):
    now = now or datetime.now(UTC)
    with factory() as db:
        occurrence = db.get(TrackingOccurrence, occurrence_id)
        if not occurrence or occurrence.status != "running" or occurrence.lease_token != token:
            return {"status": "not_claimed"}
        schedule = db.get(TrackingSchedule, occurrence.schedule_id)
        config = db.get(CapabilityConfig, schedule.config_id)
        try:
            actor = eligible(db, config, schedule.created_by)
            if schedule.status != "active":
                raise ValueError("追踪计划已经停止")
            if occurrence.run_id:
                run = db.get(CapabilityRun, occurrence.run_id)
                if run.input_snapshot["config"] != config.config:
                    raise ValueError("重试期间配置已改变，请新建追踪计划")
            else:
                run = CapabilityRun(
                    config_id=config.id,
                    research_case_id=config.research_case_id,
                    requested_by=actor.id,
                    status="running",
                    started_at=now,
                    config_revision_id=db.scalar(
                        select(CapabilityConfigRevision.id)
                        .where(CapabilityConfigRevision.config_id == config.id)
                        .order_by(CapabilityConfigRevision.revision_no.desc())
                        .limit(1)
                    ),
                    input_snapshot={
                        "config": copy.deepcopy(config.config),
                        "tracking_occurrence_id": occurrence.id,
                        "output_prompt": schedule.output_prompt,
                    },
                    output={},
                )
                db.add(run)
                db.flush()
                occurrence.run_id = run.id
            db.commit()
            with db.begin_nested():
                # Hold the occurrence lock while producing observations; scanner uses SKIP LOCKED.
                occurrence = db.scalar(
                    select(TrackingOccurrence)
                    .where(TrackingOccurrence.id == occurrence_id, TrackingOccurrence.lease_token == token)
                    .with_for_update()
                )
                if not occurrence:
                    raise ValueError("执行租约已变化")
                output = (
                    aggregate_summary(db, schedule, run)
                    if occurrence.kind == "summary"
                    else tracking_workflow.execute(db, run, run.input_snapshot["config"], client=client)
                )
                # Rights and schedule status are checked again before making any artifact available.
                db.refresh(schedule)
                db.refresh(config)
                eligible(db, config, actor.id)
                if schedule.status != "active":
                    raise ValueError("追踪计划已经停止")
                output["schedule"] = {
                    "kind": occurrence.kind,
                    "scheduled_at": occurrence.scheduled_at.isoformat(),
                    "actual_at": now.isoformat(),
                    "covered_slots": occurrence.covered_slots,
                }
                run.output, run.status, run.finished_at = output, "succeeded", max(now, datetime.now(UTC))
                run.error_code = run.error_message = None
                # Readable drafts are generated only for a meaningful update, or the final summary.
                if output.get("meaningful_changes") or occurrence.kind == "summary":
                    document = workflow_documents.initial_document(db, run)
                    if occurrence.kind == "summary":
                        document["title"] = document["title"].replace("专题更新简报", "专题追踪总结")
                        document["markdown"] = document["markdown"].replace("专题更新简报", "专题追踪总结")
                        document["markdown"] += (
                            f"\n\n追踪范围：{schedule.start_date} 至 {schedule.end_date}。"
                            f"失败轮次：{len(output.get('failed_occurrences', []))}。"
                        )
                    document = workflow_documents.apply_tracking_prompt(
                        db, run, document, schedule.output_prompt
                    )
                    draft = workflow_documents.append(
                        db, run, actor.id, document, "draft", "自动生成待审草稿；未确认采用"
                    )
                    occurrence.draft_revision_id = draft.id
                if not db.scalar(
                    select(TrackingDelivery.id).where(TrackingDelivery.occurrence_id == occurrence.id)
                ):
                    db.add(
                        TrackingDelivery(
                            occurrence_id=occurrence.id,
                            research_case_id=run.research_case_id,
                            observation_ids=[
                                x["observation_id"]
                                for x in output.get("observations", [])
                                if x.get("observation_id")
                            ],
                        )
                    )
                from app.models.project_workspace import ProjectEvidence

                for observation in output.get("observations", []):
                    snapshot = observation["snapshot"]
                    if (
                        occurrence.kind == "summary"
                        or observation.get("observation") not in {"new", "updated"}
                        or snapshot.get("match_status") != "matched"
                        or snapshot.get("access_status") != "accessible"
                    ):
                        continue
                    oid = observation["observation_id"]
                    if not db.scalar(
                        select(ProjectEvidence.id).where(
                            ProjectEvidence.research_case_id == run.research_case_id,
                            ProjectEvidence.object_type == "tracking_observation",
                            ProjectEvidence.object_id == oid,
                        )
                    ):
                        db.add(
                            ProjectEvidence(
                                research_case_id=run.research_case_id,
                                object_type="tracking_observation",
                                object_id=oid,
                                snapshot={
                                    **snapshot,
                                    "country_iso3": config.config.get("country_iso3"),
                                    "locator": {"observation_id": oid, "run_id": run.id},
                                    "boundary": "专题追踪本轮观察，人工采用前不进入正式成果",
                                },
                                decision="pending",
                            )
                        )
                # An entirely unavailable source set is a failure, never a false 'no updates'.
                if (
                    output.get("source_failures")
                    and not any(
                        o["snapshot"].get("access_status") == "accessible"
                        for o in output.get("observations", [])
                    )
                    and occurrence.kind != "summary"
                ):
                    raise ValueError("本轮所有来源获取失败；未生成更新简报")
            occurrence.status, occurrence.finished_at = "succeeded", max(now, datetime.now(UTC))
            occurrence.lease_token = occurrence.lease_expires_at = None
            if occurrence.kind == "summary":
                schedule.status, schedule.finished_at = "completed", datetime.now(UTC)
            db.commit()
            return {
                "status": occurrence.status,
                "run_id": run.id,
                "draft_revision_id": occurrence.draft_revision_id,
            }
        except Exception as exc:
            db.rollback()
            occurrence = db.scalar(
                select(TrackingOccurrence).where(TrackingOccurrence.id == occurrence_id).with_for_update()
            )
            if occurrence.lease_token != token:
                return {"status": "lease_changed"}
            message = str(exc)[:300] if isinstance(exc, ValueError) else "执行失败，请检查本机工作进程记录"
            occurrence.errors = [
                *occurrence.errors,
                {"attempt": occurrence.attempts, "at": datetime.now(UTC).isoformat(), "reason": message},
            ]
            occurrence.status = "queued" if occurrence.attempts < MAX_ATTEMPTS else "failed"
            occurrence.available_at = datetime.now(UTC) + timedelta(minutes=occurrence.attempts)
            occurrence.lease_token = occurrence.lease_expires_at = None
            if occurrence.run_id:
                run = db.get(CapabilityRun, occurrence.run_id)
                run.status, run.error_code, run.error_message = "failed", "tracking_failed", message
                run.finished_at = max(now, datetime.now(UTC))
            db.commit()
            return {"status": occurrence.status, "error": message}
