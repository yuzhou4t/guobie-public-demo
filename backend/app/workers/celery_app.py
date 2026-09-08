from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "guobie",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)
celery_app.conf.update(
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    result_serializer="json",
    task_serializer="json",
    timezone="UTC",
    beat_schedule={
        "tracking-scan-every-minute": {"task": "guobie.scan_tracking_schedules", "schedule": 60.0}
    },
    task_routes={"guobie.*tracking*": {"queue": "tracking"}},
)
