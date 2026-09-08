"""Read-only snapshots explicitly prepared for the local demonstration."""

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings

router = APIRouter(prefix="/api/v1/reader", tags=["local-demo"])
DEMO_PATH = Path(__file__).resolve().parents[3] / "var" / "demo" / "field-library.json"


def available(request: Request) -> bool:
    settings = get_settings()
    return bool(
        settings.environment == "development"
        and not settings.public_api_read_only
        and request.client
        and request.client.host in {"127.0.0.1", "::1"}
        and DEMO_PATH.is_file()
    )


@router.get("/field-demo")
def field_demo(request: Request):
    if not available(request):
        raise HTTPException(404, "Demo unavailable")
    return JSONResponse(json.loads(DEMO_PATH.read_text()), headers={"Cache-Control": "no-store"})
