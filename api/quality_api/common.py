from __future__ import annotations

import hashlib
import json
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .auth import Principal
from .errors import APIError
from .models import Audit, Case, Idempotency, Report, Run, Snapshot

REQUEST_ID = ContextVar("quality_request_id", default="background")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError):
        raise APIError(422, "SCHEMA_INVALID", "유한한 값으로 구성된 JSON이 필요합니다.") from None
    return hashlib.sha256(encoded).hexdigest()


def ok(data):
    return {"data": data, "request_id": REQUEST_ID.get()}


def serialize_key(db: Session, key: str):
    if db.bind.dialect.name == "postgresql":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def case_for(db: Session, id: str, p: Principal, lock: bool = False, allow_deleted: bool = False) -> Case:
    query = select(Case).where(Case.id == id)
    if lock:
        query = query.with_for_update()
    c = db.scalar(query)
    if not c or (c.owner_id != p.user_id and (not c.team_id or c.team_id not in p.team_ids)):
        raise APIError(404, "NOT_FOUND", "업무 케이스를 찾을 수 없습니다.")
    if c.instance_id != p.instance_id or (p.case_id is not None and c.id != p.case_id):
        raise APIError(404, "NOT_FOUND", "업무 케이스를 찾을 수 없습니다.")
    if p.kind in {"model_delegate", "trace_adapter"} and p.chat_id is not None and c.chat_id != p.chat_id:
        raise APIError(404, "NOT_FOUND", "업무 케이스를 찾을 수 없습니다.")
    if c.deleted_at is not None and not allow_deleted:
        raise APIError(404, "NOT_FOUND", "업무 케이스를 찾을 수 없습니다.")
    return c


def run_for(db: Session, id: str, p: Principal, lock: bool = False):
    run = db.get(Run, id)
    if not run or (p.run_id is not None and p.run_id != id):
        raise APIError(404, "NOT_FOUND", "실행 기록을 찾을 수 없습니다.")
    c = case_for(db, run.case_id, p, lock=lock)
    if lock:
        db.refresh(run)
    return run, c


def assert_active(c: Case):
    if c.deleted_at is not None or c.lifecycle != "active":
        raise APIError(409, "INVALID_STATE", "업무를 다시 연 뒤 변경해 주세요.")


def assert_revision(c: Case, expected: int):
    if c.revision != expected:
        raise APIError(409, "REVISION_CONFLICT", "내용이 변경되었습니다. 최신 내용을 확인하세요.", {"current_revision": c.revision})


def bump(c: Case):
    c.revision += 1
    c.updated_at = utcnow()


def audit(db: Session, p: Principal, action: str, entity_id: str,
          case_id: str | None = None, details: dict | None = None):
    db.add(Audit(id=new_id("audit"), actor_id=p.user_id, actor_kind=p.kind,
                 action=action, entity_id=entity_id, case_id=case_id, details=details or {}))


def idempotent(db: Session, p: Principal, operation: str, entity: str,
               key: str, payload: dict, fn: Callable):
    if not key or len(key) > 200:
        raise APIError(422, "SCHEMA_INVALID", "유효한 Idempotency-Key가 필요합니다.")
    scope = digest([p.kind, p.subject, p.user_id, p.instance_id, p.epoch])
    request_hash = digest(payload)
    serialize_key(db, "idem:" + digest([scope, operation, entity, key]))
    row = db.scalar(select(Idempotency).where(Idempotency.scope == scope, Idempotency.operation == operation,
                                            Idempotency.entity == entity, Idempotency.key == key))
    if row:
        if row.payload_hash != request_hash:
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "같은 요청 키에 다른 내용이 전달되었습니다.")
        if row.response is None:
            raise APIError(409, "REQUEST_PENDING", "기존 요청이 처리 중입니다.")
        return row.response
    row = Idempotency(id=new_id("idem"), scope=scope, operation=operation, entity=entity,
                      key=key, payload_hash=request_hash, response=None)
    db.add(row)
    db.flush()
    result = fn()
    row.response = result
    db.flush()
    return result


def invalidate(db: Session, c: Case, evidence_id: str | None = None):
    for snap in db.scalars(select(Snapshot).where(Snapshot.case_id == c.id, Snapshot.state == "accepted")):
        refs = snap.body.get("evidence_versions", snap.body.get("evidence", []))
        if evidence_id is not None and not any(item.get("evidence_id", item.get("id")) == evidence_id for item in refs):
            continue
        snap.state = "superseded"
        for report in db.scalars(select(Report).where(Report.snapshot_id == snap.id)):
            report.outdated = True
