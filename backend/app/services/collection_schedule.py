from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CollectionRun, SourceChannel

BEIJING = ZoneInfo("Asia/Shanghai")

SCHEDULE_GROUPS = (
    "metadata-daily",
    "metadata-weekly",
    "metadata-monthly",
    "structured",
)
STALE_COLLECTION_RUN_AFTER = timedelta(hours=6)


def recover_stale_collection_runs(
    session: Session,
    *,
    now: datetime,
    stale_after: timedelta = STALE_COLLECTION_RUN_AFTER,
) -> tuple[int, ...]:
    """Fail abandoned active runs so a later scheduled run can claim the channel."""

    if stale_after <= timedelta(0):
        raise ValueError("stale_after must be positive")
    cutoff = _as_utc(now) - stale_after
    recovered: list[int] = []
    active_runs = list(
        session.scalars(
            select(CollectionRun)
            .where(CollectionRun.status.in_(("queued", "running")))
            .order_by(CollectionRun.id)
        )
    )
    for run in active_runs:
        last_activity = run.heartbeat_at or run.started_at or run.queued_at or run.created_at
        if last_activity is None or _as_utc(last_activity) >= cutoff:
            continue
        previous_status = run.status
        run.status = "failed"
        run.finished_at = now
        run.heartbeat_at = now
        run.error_category = "internal"
        run.error_code = "stale_run_recovered"
        run.error_message = (
            f"{previous_status} collection run had no heartbeat for more than "
            f"{int(stale_after.total_seconds())} seconds and was released by the scheduler"
        )
        run.report = {
            **(run.report or {}),
            "stale_recovery": {
                "previous_status": previous_status,
                "last_activity_at": last_activity.isoformat(),
                "recovered_at": now.isoformat(),
            },
        }
        recovered.append(run.id)
    session.flush()
    return tuple(recovered)


def is_current_cadence_period(
    value: datetime | None,
    cadence: str,
    *,
    now: datetime,
) -> bool:
    """Return whether a completed run belongs to the current Beijing cadence period."""

    if cadence == "on_demand":
        return True
    if value is None:
        return False

    local_value = _as_beijing(value)
    local_now = _as_beijing(now)
    if cadence == "daily":
        return local_value.date() == local_now.date()
    if cadence == "weekly":
        return local_value.isocalendar()[:2] == local_now.isocalendar()[:2]
    if cadence == "monthly":
        return (local_value.year, local_value.month) == (local_now.year, local_now.month)
    raise ValueError(f"unsupported cadence: {cadence}")


def due_collection_groups(
    session: Session,
    group_rows: Mapping[str, Sequence[int]],
    *,
    now: datetime,
) -> tuple[str, ...]:
    """Select groups with at least one live channel lacking a success this period."""

    unsupported = set(group_rows) - set(SCHEDULE_GROUPS)
    if unsupported:
        raise ValueError(f"unsupported collection groups: {sorted(unsupported)}")

    channels = list(
        session.scalars(
            select(SourceChannel).where(SourceChannel.status != "retired").order_by(SourceChannel.id)
        )
    )
    channels_by_row: dict[int, list[SourceChannel]] = {}
    for channel in channels:
        excel_row = channel.collector_config.get("catalog_excel_row")
        if isinstance(excel_row, int):
            channels_by_row.setdefault(excel_row, []).append(channel)

    selected_ids = {
        channel.id for rows in group_rows.values() for row in rows for channel in channels_by_row.get(row, ())
    }
    latest_success = dict(
        session.execute(
            select(CollectionRun.channel_id, func.max(CollectionRun.finished_at))
            .where(
                CollectionRun.channel_id.in_(selected_ids),
                CollectionRun.status == "succeeded",
                CollectionRun.finished_at.is_not(None),
            )
            .group_by(CollectionRun.channel_id)
        ).all()
    )

    due: list[str] = []
    for group in SCHEDULE_GROUPS:
        rows = group_rows.get(group)
        if rows is None:
            continue
        selected = [channel for row in rows for channel in channels_by_row.get(row, ())]
        if not selected:
            raise RuntimeError(f"collection group {group} has no live database channels")
        cadence = group.removeprefix("metadata-") if group.startswith("metadata-") else "monthly"
        if any(
            not is_current_cadence_period(latest_success.get(channel.id), cadence, now=now)
            for channel in selected
        ):
            due.append(group)
    return tuple(due)


def _as_beijing(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING)
    return value.astimezone(BEIJING)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def rate_limit_cooldown(run: CollectionRun, now: datetime) -> bool:
    """A missing Retry-After defers a limited source until the next Beijing day."""
    from email.utils import parsedate_to_datetime

    if run.error_code != "http_429" or not run.finished_at:
        return False
    value = (run.report or {}).get("retry_after")
    if value:
        try:
            until = (
                _as_utc(run.finished_at) + timedelta(seconds=max(0, int(value)))
                if str(value).isdigit()
                else _as_utc(parsedate_to_datetime(value))
            )
            return _as_utc(now) < until
        except (ValueError, TypeError, OverflowError):
            pass
    return _as_beijing(run.finished_at).date() >= _as_beijing(now).date()
