"""S05-only model boundary. Generic research writing still excludes field originals."""

from contextlib import contextmanager
from contextvars import ContextVar

from app.models import User
from app.services import field_access

_CHECK = ContextVar("field_ai_permission_check", default=None)


def checkpoint():
    check = _CHECK.get()
    if check:
        check()


@contextmanager
def authorized_source(db, run, material_id, *, actor_id=None):
    previous_check = _CHECK.get()

    def check():
        if previous_check:
            previous_check()
        actor = db.get(User, actor_id or run.requested_by, populate_existing=True)
        return field_access.resolve_field_material_access(db, actor, material_id, run.research_case_id, "ai")

    access = check()
    field_access.bind_run(db, run, access)
    token = _CHECK.set(check)
    try:
        yield access
        check()
    finally:
        _CHECK.reset(token)
