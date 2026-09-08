from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter(tags=["health"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/ready")
def ready(db: DbSession) -> dict[str, str]:
    try:
        db.execute(text("select 1"))
        if not inspect(db.get_bind()).has_table("sources"):
            raise HTTPException(status_code=503, detail="database schema is not migrated")
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}
