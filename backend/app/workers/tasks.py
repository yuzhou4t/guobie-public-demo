from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models import CollectionItem, CollectionRun, SourceChannel
from app.services.source_probe import (
    ProbeError,
    SafeHttpClient,
    classify_content,
    normalize_http_url,
)
from app.workers.celery_app import celery_app

SessionFactory = Callable[[], Session]


def _safe_normalized_url(url: str) -> str:
    try:
        return normalize_http_url(url)
    except ProbeError:
        return url


def _failure_channel_status(channel: SourceChannel, error: ProbeError, prior_status: str) -> str:
    if error.category in {"access", "policy"}:
        return "blocked"
    if prior_status in {"active", "paused"}:
        return prior_status
    return "draft"


def execute_source_probe(
    run_id: int,
    *,
    session_factory: SessionFactory | None = None,
    client: SafeHttpClient | None = None,
) -> dict[str, object]:
    make_session = session_factory or get_session_factory()
    probe_client = client or SafeHttpClient()

    with make_session() as db:
        run = db.scalar(select(CollectionRun).where(CollectionRun.id == run_id).with_for_update())
        if run is None:
            raise ValueError(f"collection run {run_id} does not exist")
        if run.status != "queued":
            return dict(run.report)
        channel = db.get(SourceChannel, run.channel_id)
        if channel is None:
            raise ValueError(f"source channel {run.channel_id} does not exist")

        prior_status = str(run.report.get("channel_status_before", "draft"))
        now = datetime.now(UTC)
        run.status = "running"
        run.started_at = now
        run.heartbeat_at = now
        db.commit()

        try:
            resource = probe_client.fetch(channel.entry_url)
            detected_type = classify_content(resource)
            item = CollectionItem(
                run_id=run.id,
                request_url=resource.request_url,
                normalized_url=_safe_normalized_url(resource.final_url),
                discovered_from_url=None,
                status="fetched",
                http_status=resource.status_code,
            )
            db.add(item)
            run.items_discovered = 1
            run.items_persisted = 0
            run.finished_at = datetime.now(UTC)
            run.heartbeat_at = run.finished_at
            run.report = {
                "channel_status_before": prior_status,
                "expected_type": channel.collector_type,
                "detected_type": detected_type,
                "final_url": resource.final_url,
                "content_type": resource.content_type,
                "byte_size": len(resource.body),
                "sha256": resource.sha256,
                "etag": resource.etag,
                "last_modified": resource.last_modified,
            }

            if detected_type == "unknown":
                run.status = "failed"
                run.error_category = "parser"
                run.error_code = "needs_manual_adapter"
                run.error_message = "response is safe to fetch but its structure is not recognized"
                channel.status = "blocked"
            elif detected_type != channel.collector_type:
                run.status = "failed"
                run.error_category = "parser"
                run.error_code = "collector_type_mismatch"
                run.error_message = (
                    f"configured collector is {channel.collector_type}, "
                    f"but the response looks like {detected_type}"
                )
                channel.status = "blocked"
            else:
                run.status = "succeeded"
                if prior_status in {"active", "paused"}:
                    channel.status = prior_status
                else:
                    channel.status = "shadow"
            db.commit()
            return dict(run.report)
        except ProbeError as error:
            run.status = "failed"
            run.finished_at = datetime.now(UTC)
            run.heartbeat_at = run.finished_at
            run.error_category = error.category
            run.error_code = error.code
            run.error_message = error.message[:1000]
            run.report = {
                "channel_status_before": prior_status,
                "retryable": error.retryable,
            }
            channel.status = _failure_channel_status(channel, error, prior_status)
            db.add(
                CollectionItem(
                    run_id=run.id,
                    request_url=channel.entry_url,
                    normalized_url=_safe_normalized_url(channel.entry_url),
                    status="failed",
                    error_category=error.category,
                    error_code=error.code,
                    error_message=error.message[:1000],
                )
            )
            db.commit()
            return dict(run.report)
        except Exception as error:
            db.rollback()
            run = db.get(CollectionRun, run_id)
            channel = db.get(SourceChannel, run.channel_id) if run is not None else None
            if run is None or channel is None:
                raise
            run.status = "failed"
            run.finished_at = datetime.now(UTC)
            run.heartbeat_at = run.finished_at
            run.error_category = "internal"
            run.error_code = "unexpected_probe_error"
            run.error_message = str(error)[:1000]
            run.report = {
                "channel_status_before": prior_status,
                "retryable": False,
            }
            channel.status = prior_status if prior_status != "probing" else "draft"
            db.commit()
            raise


@celery_app.task(name="guobie.probe_source_channel")
def probe_source_channel(run_id: int) -> dict[str, object]:
    return execute_source_probe(run_id)


@celery_app.task(name="guobie.scan_tracking_schedules", ignore_result=True)
def scan_tracking_schedules_task():
    from app.services.tracking_scheduler import enqueue_due

    with get_session_factory()() as db:
        queued = enqueue_due(db)
        db.commit()
    for occurrence_id in queued:
        execute_tracking_occurrence_task.apply_async(args=[occurrence_id], retry=False)


@celery_app.task(
    name="guobie.execute_tracking_occurrence",
    ignore_result=True,
    soft_time_limit=900,
    time_limit=960,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_tracking_occurrence_task(occurrence_id):
    from app.services.tracking_scheduler import claim, execute_occurrence

    factory = get_session_factory()
    with factory() as db:
        token = claim(db, occurrence_id)
        db.commit()
    if token:
        return execute_occurrence(factory, occurrence_id, token)
    return {"status": "already_claimed_or_unavailable"}
