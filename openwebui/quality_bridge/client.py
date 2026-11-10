"""Small, role-separated client for admin-installed Open WebUI plugins.

Only Open WebUI injected __user__ / chat context may be passed here. This
library is not an HTTP authentication endpoint and never accepts identity
from the model's tool arguments. Installed Python plugins are trusted code.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import os
import re
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx
import jwt


class BridgeError(RuntimeError):
    """Safe error text: upstream request bodies, tokens and results excluded."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def verified_user(user: Mapping[str, Any] | None) -> str:
    """Validate the *injected* record; Open WebUI itself authenticates it.

    A mapping passed in tool JSON is never an acceptable substitute. Pending
    users cannot enter the quality application; admin confers no case ACL.
    """
    if not isinstance(user, Mapping) or user.get("role") not in {"user", "admin"}:
        raise BridgeError("UNAUTHENTICATED", "로그인된 Open WebUI 사용자만 사용할 수 있습니다.")
    return context_id(user.get("id"), "user_id")


def context_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,200}", value):
        raise BridgeError("CONTEXT_MISSING", f"신뢰된 {label} 맥락을 확인할 수 없습니다.")
    if value in {"local", "undefined", "null"}:
        raise BridgeError("CONTEXT_MISSING", "저장된 대화에서 실행하세요.")
    return value


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise BridgeError("RESULT_NOT_JSON", "결과를 유효한 JSON으로 변환하는 어댑터가 필요합니다.") from exc


def operation_key(*values: Any) -> str:
    """For idempotent quality-state writes only, never a source invocation ID."""
    return sha256(canonical(values).encode()).hexdigest()


def _base_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BridgeError("NOT_CONFIGURED", f"{name}에 고정된 HTTP(S) 서비스 주소가 필요합니다.")
    return value.rstrip("/")


@dataclass(frozen=True)
class BridgeSettings:
    api_base_url: str
    ui_base_url: str
    instance_id: str
    secrets: Mapping[str, str] = field(repr=False)
    issuer: str = "quality-openwebui"
    audience: str = "quality-api"
    epoch: str = "1"
    timeout: float = 15.0
    environment: str = "production"
    outbox_path: str | None = None

    def __post_init__(self):
        api_base = _base_url(self.api_base_url, "QUALITY_API_BASE_URL")
        # Earlier deployment notes included the API prefix in this variable.
        # Accept that exact suffix without duplicating it in request URLs, and
        # preserve a reverse proxy's preceding path (e.g. /quality-service).
        if api_base.endswith("/quality/v1"):
            api_base = api_base[:-len("/quality/v1")]
        object.__setattr__(self, "api_base_url", api_base)
        object.__setattr__(self, "ui_base_url", _base_url(self.ui_base_url, "QUALITY_UI_BASE_URL"))
        context_id(self.instance_id, "QUALITY_INSTANCE_ID")
        if not isinstance(self.epoch, str) or not self.epoch or len(self.epoch) > 80 or self.timeout <= 0:
            raise BridgeError("NOT_CONFIGURED", "복구 세대와 HTTP 제한시간을 확인하세요.")
        configured = [value for value in self.secrets.values() if value]
        if len(set(configured)) != len(configured):
            raise BridgeError("NOT_CONFIGURED", "사람·모델·기록 서명키를 각각 다르게 설정하세요.")
        if self.environment == "production" and urlsplit(self.ui_base_url).scheme != "https":
            raise BridgeError("NOT_CONFIGURED", "운영 검토 화면은 HTTPS 공개 주소를 사용해야 합니다.")

    @classmethod
    def from_env(cls) -> "BridgeSettings":
        try:
            return cls(
                api_base_url=os.environ["QUALITY_API_BASE_URL"],
                ui_base_url=os.environ["QUALITY_UI_BASE_URL"],
                instance_id=os.environ["QUALITY_INSTANCE_ID"],
                secrets={
                    "human": os.getenv("QUALITY_HUMAN_JWT_SECRET", ""),
                    "model_delegate": os.getenv("QUALITY_MODEL_JWT_SECRET", ""),
                    "trace_adapter": os.getenv("QUALITY_TRACE_JWT_SECRET", ""),
                },
                issuer=os.getenv("QUALITY_JWT_ISSUER", "quality-openwebui"),
                audience=os.getenv("QUALITY_JWT_AUDIENCE", "quality-api"),
                epoch=os.environ["QUALITY_RECOVERY_EPOCH"],
                timeout=float(os.getenv("QUALITY_HTTP_TIMEOUT_SECONDS", "15")),
                environment=os.getenv("QUALITY_ENVIRONMENT", "production"),
                outbox_path=os.getenv("QUALITY_OUTBOX_PATH") or None,
            )
        except (KeyError, ValueError) as exc:
            raise BridgeError("NOT_CONFIGURED", "품질 업무 연결 환경변수를 확인하세요.") from exc


@dataclass(frozen=True)
class BridgeClient:
    settings: BridgeSettings
    user_id: str
    kind: str
    chat_id: str | None = None
    case_id: str | None = None
    run_id: str | None = None
    transport: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def for_user(cls, user: Mapping[str, Any], kind: str, chat_id: str | None = None, **kwargs) -> "BridgeClient":
        return cls(BridgeSettings.from_env(), verified_user(user), kind,
                   context_id(chat_id, "chat_id") if chat_id is not None else None, **kwargs)

    def scoped(self, *, case_id: str | None = None, run_id: str | None = None) -> "BridgeClient":
        return replace(self, case_id=case_id or self.case_id, run_id=run_id or self.run_id)

    def _token(self) -> str:
        if self.kind not in {"human", "model_delegate", "trace_adapter"}:
            raise BridgeError("FORBIDDEN", "허용되지 않은 연결 역할입니다.")
        secret = self.settings.secrets.get(self.kind, "")
        if len(secret) < 32:
            raise BridgeError("NOT_CONFIGURED", "연결 역할에 필요한 서명키가 설정되지 않았습니다.")
        now = int(time.time())
        claims = {
            "sub": f"openwebui:{self.settings.instance_id}:{self.kind}",
            "user_id": context_id(self.user_id, "user_id"), "kind": self.kind,
            "iss": self.settings.issuer, "aud": self.settings.audience,
            "iat": now, "exp": now + 60, "epoch": self.settings.epoch,
            "instance_id": self.settings.instance_id,
        }
        for name in ("chat_id", "case_id", "run_id"):
            value = getattr(self, name)
            if value is not None:
                claims[name] = context_id(value, name)
        return jwt.encode(claims, secret, algorithm="HS256", headers={"kid": self.kind})

    async def request(self, method: str, path: str, *, body: dict | None = None,
                      key: str | None = None, params: dict | None = None) -> Any:
        # All paths originate in installed code, not arbitrary model URLs.
        if not path.startswith("/") or ".." in path or "://" in path or "?" in path or "#" in path:
            raise BridgeError("INVALID_PATH", "등록된 품질 API 경로만 호출할 수 있습니다.")
        headers = {"Authorization": f"Bearer {self._token()}"}
        if key:
            headers["Idempotency-Key"] = key
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout, follow_redirects=False,
                                         transport=self.transport, trust_env=True) as http:
                response = await http.request(method, self.settings.api_base_url + "/quality/v1" + path,
                                              json=body, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise BridgeError("TRACE_UNAVAILABLE", "품질 업무 API에 연결하지 못했습니다. 원천 호출을 자동 재시도하지 않습니다.", retryable=True) from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise BridgeError("INVALID_RESPONSE", "품질 업무 API 응답 형식을 확인하세요.") from exc
        if response.is_error or response.is_redirect:
            code = result.get("error", {}).get("code", "API_ERROR") if isinstance(result, dict) else "API_ERROR"
            # Do not expose arbitrary upstream message; exception may reach model.
            safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,80}", code) else "API_ERROR"
            raise BridgeError(safe_code, f"품질 업무 요청이 거절되었습니다 ({safe_code}).", retryable=response.status_code >= 500)
        if not isinstance(result, dict) or "data" not in result:
            raise BridgeError("INVALID_RESPONSE", "품질 업무 API 응답에 data가 없습니다.")
        return result["data"]

    async def current_case(self) -> dict | None:
        if self.chat_id is None:
            raise BridgeError("CONTEXT_MISSING", "저장된 대화의 chat_id가 필요합니다.")
        if self.kind != "human":
            data = await self.request("GET", "/context/current")
            if data.get("case") is None:
                return None
            # Derived from the trusted API, never from model-supplied context.
            return {**data["case"], "context_run_id": data.get("run_id")}
        data = await self.request("GET", "/cases", params={"chat_id": self.chat_id})
        rows = data if isinstance(data, list) else data.get("items", [])
        if len(rows) > 1:
            raise BridgeError("AMBIGUOUS_CASE", "대화 연결 케이스가 중복되어 확인이 필요합니다.")
        return rows[0] if rows else None


def safe_error(exc: BridgeError) -> dict:
    return {"error": {"code": exc.code, "message": str(exc)}, "source_reexecuted": False}
