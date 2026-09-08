"""Explicitly preview and start country tracking; no automatic adoption or publication."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.api.research import (
    DbSession,
    ReaderActor,
    _case_permission,
    require_internal_reader_access,
    require_reader_write_access,
)
from app.models import CapabilityConfig
from app.models.tracking_schedule import TrackingOccurrence, TrackingSchedule
from app.services import tracking_scheduler

router = APIRouter(
    prefix="/api/v1/reader/tracking-schedules",
    tags=["tracking-schedules"],
    dependencies=[Depends(require_internal_reader_access)],
)
WRITE = [Depends(require_reader_write_access)]


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date = Field(default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai")).date())
    end_date: date = Field(
        default_factory=lambda: datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=6)
    )
    interval_days: int = Field(default=1, ge=1, le=365)
    local_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(default="Asia/Shanghai", max_length=80)
    output_prompt: str = Field(
        default="围绕追踪专题归纳本轮新增与实质变化，注明来源、获取时间与待核实事项。",
        min_length=2,
        max_length=2000,
    )

    @model_validator(mode="after")
    def valid(self):
        try:
            ZoneInfo(self.timezone)
            tracking_scheduler.planned_times(
                self.start_date, self.end_date, self.interval_days, self.local_time, self.timezone
            )
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("请核对日期范围、执行间隔和时区") from exc
        return self


@router.post("/preview")
def preview(body: ScheduleInput, actor: ReaderActor):
    now = datetime.now(UTC)
    slots = tracking_scheduler.planned_times(
        body.start_date, body.end_date, body.interval_days, body.local_time, body.timezone
    )
    return {
        "baseline": "启动后立即建立基线",
        "dates": [x.astimezone(ZoneInfo(body.timezone)).isoformat() for x in slots if x > now],
        "timezone": body.timezone,
        "runtime_note": "本机休眠或关机时无法执行，恢复后合并遗漏时段补跑一次。",
    }


@router.post("/configs/{config_id}/start", dependencies=WRITE)
def start(config_id: int, body: ScheduleInput, db: DbSession, actor: ReaderActor):
    config = db.get(CapabilityConfig, config_id)
    if not config:
        raise HTTPException(404, "追踪配置不存在")
    _case_permission(db, config.research_case_id, actor, "run_skill")
    try:
        schedule, occurrence = tracking_scheduler.start(db, config, actor, body.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    # Durable outbox is picked up by the one-minute scanner; a direct wakeup is best-effort.
    queued = False
    try:
        from app.workers.tasks import execute_tracking_occurrence_task

        execute_tracking_occurrence_task.apply_async(args=[occurrence.id], retry=False)
        queued = True
    except Exception:
        pass
    return {
        "id": schedule.id,
        "baseline_occurrence_id": occurrence.id,
        "status": "active",
        "worker_notified": queued,
        "note": "计划已保存；本机追踪工作进程运行后执行基线与定时任务。",
    }


@router.get("/configs/{config_id}")
def status(config_id: int, db: DbSession, actor: ReaderActor):
    config = db.get(CapabilityConfig, config_id)
    if not config:
        raise HTTPException(404, "配置不存在")
    _case_permission(db, config.research_case_id, actor, "view")
    schedule = db.scalar(select(TrackingSchedule).where(TrackingSchedule.config_id == config_id))
    if not schedule:
        return {"schedule": None, "occurrences": []}
    return {
        "schedule": {
            "id": schedule.id,
            "status": schedule.status,
            "start_date": schedule.start_date,
            "end_date": schedule.end_date,
            "interval_days": schedule.interval_days,
            "local_time": schedule.local_time,
            "timezone": schedule.timezone,
            "output_prompt": schedule.output_prompt,
            "next_run_at": schedule.next_run_at,
        },
        "occurrences": [
            {
                "id": o.id,
                "kind": o.kind,
                "scheduled_at": o.scheduled_at,
                "covered_slots": o.covered_slots,
                "status": o.status,
                "attempts": o.attempts,
                "run_id": o.run_id,
                "draft_revision_id": o.draft_revision_id,
                "started_at": o.started_at,
                "finished_at": o.finished_at,
                "errors": o.errors,
            }
            for o in db.scalars(
                select(TrackingOccurrence)
                .where(TrackingOccurrence.schedule_id == schedule.id)
                .order_by(TrackingOccurrence.id.desc())
                .limit(100)
            )
        ],
    }


@router.post("/{schedule_id}/stop", dependencies=WRITE)
def stop(schedule_id: int, db: DbSession, actor: ReaderActor):
    schedule = db.scalar(select(TrackingSchedule).where(TrackingSchedule.id == schedule_id).with_for_update())
    if not schedule:
        raise HTTPException(404, "计划不存在")
    config = db.get(CapabilityConfig, schedule.config_id)
    _case_permission(db, config.research_case_id, actor, "run_skill")
    schedule.status = "stopped"
    schedule.next_run_at = None
    db.commit()
    return {"status": "stopped", "note": "已停止后续执行；在途任务会在保存成果前再次检查。"}
