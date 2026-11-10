from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
from fastapi import Request

from .config import Settings
from .errors import APIError


@dataclass(frozen=True)
class Principal:
    user_id: str
    kind: str
    subject: str
    instance_id: str
    chat_id: str | None = None
    case_id: str | None = None
    run_id: str | None = None
    team_ids: tuple[str, ...] = ()
    is_curator: bool = False
    epoch: str = ""


def require_kind(p: Principal, *kinds: str):
    if p.kind not in kinds:
        raise APIError(403, "FORBIDDEN", "이 작업을 수행할 권한이 없습니다.")


def issue_token(settings: Settings, *, kind: str, user_id: str,
                instance_id: str, subject: str | None = None, chat_id: str | None = None,
                case_id: str | None = None, run_id: str | None = None, ttl: int | None = None) -> str:
    if kind not in settings.signing_keys:
        raise ValueError("Unknown principal kind")
    now = int(time.time())
    claims = {"sub": subject or f"quality:{kind}", "user_id": user_id, "kind": kind,
              "instance_id": instance_id, "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
              "iat": now, "exp": now + (ttl or settings.token_ttl_seconds), "epoch": settings.recovery_epoch}
    for key, value in {"chat_id": chat_id, "case_id": case_id, "run_id": run_id}.items():
        if value is not None:
            claims[key] = value
    return jwt.encode(claims, settings.signing_keys[kind], algorithm="HS256", headers={"kid": kind})


def principal(request: Request) -> Principal:
    settings = request.app.state.settings
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer ") or len(auth) > 8192:
        raise APIError(401, "UNAUTHENTICATED", "Open WebUI에서 다시 연결해 주세요.")
    try:
        token = auth[7:]
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if header.get("alg") != "HS256" or kid not in settings.signing_keys:
            raise ValueError("Invalid token algorithm/key")
        c = jwt.decode(token, settings.signing_keys[kid], algorithms=["HS256"],
                       audience=settings.jwt_audience, issuer=settings.jwt_issuer,
                       leeway=3, options={"require": ["sub", "user_id", "kind", "instance_id", "iat", "exp", "epoch"]})
        if c["kind"] != kid or c["epoch"] != settings.recovery_epoch:
            raise ValueError("Role or recovery epoch mismatch")
        if not isinstance(c["iat"], int) or not isinstance(c["exp"], int) or not 0 < c["exp"] - c["iat"] <= 300:
            raise ValueError("Token lifetime exceeded")
        for key in ("sub", "user_id", "instance_id"):
            if not isinstance(c[key], str) or not 0 < len(c[key]) <= 200:
                raise ValueError("Invalid identity")
        for key in ("chat_id", "case_id", "run_id"):
            if key in c and (not isinstance(c[key], str) or not 0 < len(c[key]) <= 200):
                raise ValueError("Invalid scope")
        if kid in {"model_delegate", "trace_adapter"} and not c.get("case_id"):
            if not (c.get("chat_id") and request.method == "GET" and request.url.path == "/quality/v1/context/current"):
                raise ValueError("Delegation needs a case scope")
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise APIError(401, "UNAUTHENTICATED", "인증이 만료되었거나 연결 정보가 유효하지 않습니다.") from None
    return Principal(user_id=c["user_id"], kind=kid, subject=c["sub"], instance_id=c["instance_id"],
                     chat_id=c.get("chat_id"), case_id=c.get("case_id"), run_id=c.get("run_id"),
                     team_ids=tuple(settings.user_teams.get(c["user_id"], [])),
                     is_curator=c["user_id"] in settings.curator_ids, epoch=c["epoch"])
