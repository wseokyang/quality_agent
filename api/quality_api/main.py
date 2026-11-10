from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update

from . import __version__
from .auth import Principal, issue_token, principal, require_kind
from .common import REQUEST_ID, aware, new_id, utcnow
from .config import Settings
from .db import Database, get_db
from .errors import APIError
from .models import UITicket

SCHEMA_VERSION = "0001"
logger = logging.getLogger("quality_api")


class TicketExchange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: Annotated[str, Field(min_length=20, max_length=200)]


class DrainState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def create_app(settings: Settings | None = None, database: Database | None = None):
    settings = settings or Settings()
    database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        settings.artifact_dir.mkdir(parents=True, exist_ok=True)
        yield
        database.engine.dispose()

    app = FastAPI(title="품질 업무 API", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.environment != "production" else None,
                  redoc_url=None, openapi_url="/openapi.json" if settings.environment != "production" else None)
    app.state.settings = settings
    app.state.database = database
    app.state.draining = settings.draining

    def error_response(request, status, code, message, details=None):
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message,
                            "retryable": False, "details": details or {}},
                            "request_id": getattr(request.state, "request_id", new_id("req"))})

    @app.middleware("http")
    async def envelope(request, call_next):
        request.state.request_id = new_id("req")
        REQUEST_ID.set(request.state.request_id)
        if request.method in {"POST", "PUT", "PATCH"}:
            parts, length = [], 0
            async for chunk in request.stream():
                length += len(chunk)
                if length > settings.max_request_bytes:
                    return error_response(request, 413, "PAYLOAD_TOO_LARGE", "요청 크기 제한을 초과했습니다.")
                parts.append(chunk)
            request._body = b"".join(parts)
            if request._body and "application/json" in request.headers.get("content-type", ""):
                try:
                    def invalid_constant(_):
                        raise ValueError("non-finite")
                    json.loads(request._body, parse_constant=invalid_constant)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    return error_response(request, 422, "SCHEMA_INVALID", "유효한 JSON 요청이 필요합니다.")
        mutating = request.method not in {"GET", "HEAD", "OPTIONS"}
        if settings.recovery_mode and mutating and not request.url.path.startswith("/quality/v1/ui/"):
            return error_response(request, 503, "RECOVERY_MODE", "복구 확인 중에는 새 작업을 실행할 수 없습니다.")
        if app.state.draining and mutating and (request.url.path.endswith("/runs") or request.url.path.endswith("/calls/claim")):
            return error_response(request, 503, "DRAINING", "업데이트 중입니다. 진행 중인 결과만 저장합니다.")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(APIError)
    async def domain_error(request, exc):
        return error_response(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo `input` values or full request bodies in a validation response.
        details = {"fields": [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}
        return error_response(request, 422, "SCHEMA_INVALID", "입력 형식을 확인해 주세요.", details)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logger.error("Unhandled %s for request %s", type(exc).__name__, getattr(request.state, "request_id", "unknown"))
        return error_response(request, 500, "INTERNAL_ERROR", "처리 중 오류가 발생했습니다. 요청 ID로 확인해 주세요.")

    @app.get("/health/live")
    def live():
        return {"status": "live", "version": __version__}

    @app.get("/health/ready")
    def ready():
        if settings.recovery_mode:
            return JSONResponse({"status": "not_ready", "code": "RECOVERY_MODE"}, status_code=503)
        try:
            with database.engine.connect() as conn:
                version = conn.scalar(text("SELECT version_num FROM alembic_version"))
                if version != SCHEMA_VERSION:
                    raise ValueError("Schema mismatch")
            if not settings.artifact_dir.is_dir() or not os.access(settings.artifact_dir, os.R_OK | os.W_OK):
                raise ValueError("Artifact path unavailable")
        except Exception:
            return JSONResponse({"status": "not_ready", "code": "DEPENDENCY_UNAVAILABLE"}, status_code=503)
        return {"status": "ready", "version": __version__, "draining": app.state.draining}

    @app.post("/quality/v1/ui/tickets")
    def ticket(request: Request, p: Annotated[Principal, Depends(principal)], db=Depends(get_db)):
        require_kind(p, "human")
        raw = secrets.token_urlsafe(32)
        row = UITicket(id=new_id("ticket"), token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                       claims={"user_id": p.user_id, "instance_id": p.instance_id, "chat_id": p.chat_id,
                               "case_id": p.case_id, "epoch": p.epoch},
                       expires_at=utcnow() + timedelta(seconds=settings.ui_ticket_ttl_seconds))
        db.add(row)
        return {"data": {"ticket": raw, "expires_in": settings.ui_ticket_ttl_seconds}, "request_id": request.state.request_id}

    @app.post("/quality/v1/ui/exchange")
    def exchange(body: TicketExchange, request: Request, db=Depends(get_db)):
        ticket_hash = hashlib.sha256(body.ticket.encode()).hexdigest()
        row = db.scalar(select(UITicket).where(UITicket.token_hash == ticket_hash).with_for_update())
        if not row or row.consumed_at or aware(row.expires_at) <= utcnow() or row.claims.get("epoch") != settings.recovery_epoch:
            raise APIError(401, "UNAUTHENTICATED", "연결 링크가 만료되었습니다. Open WebUI에서 다시 열어 주세요.")
        changed = db.execute(update(UITicket).where(UITicket.id == row.id, UITicket.consumed_at.is_(None)).values(consumed_at=utcnow()))
        if changed.rowcount != 1:
            raise APIError(401, "UNAUTHENTICATED", "이미 사용한 연결 링크입니다.")
        c = row.claims
        access = issue_token(settings, kind="human", user_id=c["user_id"], instance_id=c["instance_id"],
                             subject="quality:ui", chat_id=c.get("chat_id"), case_id=c.get("case_id"))
        context = {**c, "team_ids": settings.user_teams.get(c["user_id"], []), "is_curator": c["user_id"] in settings.curator_ids}
        return {"data": {"access_token": access, "token_type": "bearer", "expires_in": settings.token_ttl_seconds,
                          "context": context}, "request_id": request.state.request_id}

    @app.post("/quality/v1/ops/drain")
    def drain(body: DrainState, p: Annotated[Principal, Depends(principal)]):
        require_kind(p, "worker")
        if p.subject != "quality:operator":
            raise APIError(403, "FORBIDDEN", "운영자 요청이 필요합니다.")
        app.state.draining = body.enabled
        return {"data": {"draining": app.state.draining}}

    from .routes import assets, core, reports
    for router in (core.router, reports.router, assets.router):
        app.include_router(router, prefix="/quality/v1")

    @app.get("/ui", include_in_schema=False)
    def ui_redirect():
        return RedirectResponse("/ui/index.html")

    app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static"), name="quality-ui")
    return app
