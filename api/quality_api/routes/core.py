"""Authenticated quality case workflow; source execution stays in the trusted bridge.

No route in this module performs a source network call. Call claims grant execution
once, while outcomes store bounded immutable JSON observations from the recorder.
"""
from __future__ import annotations

import base64
import json
import secrets
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..auth import Principal, principal, require_kind
from ..common import (
    assert_active, assert_revision, audit, bump, case_for, digest,
    idempotent, invalidate, new_id, ok, run_for, utcnow,
)
from ..db import get_db
from ..errors import APIError
from ..models import (
    Audit, Case, Evidence, EvidenceVersion, Report, Run, Snapshot, SourceRef, ToolCall,
)

router = APIRouter()
DB = Annotated[Session, Depends(get_db)]
Actor = Annotated[Principal, Depends(principal)]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
Identifier = Annotated[str, Field(min_length=1, max_length=80)]
Revision = Annotated[int, Field(strict=True, ge=1)]

DEFAULT_PROFILE = {
    "profile_id": "quality-general", "profile_version": 1,
    "evidence_review": "included_only", "required_evidence_slots": [],
    "decision_mode": "none", "report_mode": "on_demand",
    "source_effect_policy": "read_only",
}
MAX_RESULT_BYTES = 1_048_576


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class CaseCreate(DTO):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    purpose: Annotated[str, Field(min_length=1, max_length=2000)]
    profile_id: str = "quality-general"
    profile_version: Revision = 1


class RunStart(DTO):
    request_id: Identifier
    input_ref_ids: Annotated[list[Identifier], Field(max_length=100)] = []
    user_message: Annotated[str, Field(min_length=1, max_length=20000)]
    expected_revision: Revision


class CallClaim(DTO):
    invocation_id: Identifier
    tool_id: Identifier
    tool_version: Annotated[str, Field(min_length=1, max_length=80)]
    arguments: dict[str, Any]
    expected_run_state: Literal["running"] = "running"


class CallOutcome(DTO):
    claim_token: Annotated[str, Field(min_length=20, max_length=200)]
    state: Literal["succeeded", "failed", "partial", "unknown"]
    result: Any = None
    upstream_request_id: Annotated[str | None, Field(max_length=200)] = None
    error_code: Annotated[str | None, Field(max_length=100)] = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.state in {"succeeded", "partial"} and self.result is None:
            raise ValueError("Successful and partial calls require a stored result; use [] for empty results")
        try:
            payload = json.dumps(self.result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise ValueError("Result must contain finite JSON data") from exc
        if len(payload) > MAX_RESULT_BYTES:
            raise ValueError("Inline result exceeds 1 MiB; use a bounded source adapter")
        return self


class CallReconciliation(CallOutcome):
    reconciliation_reason: Annotated[str, Field(min_length=1, max_length=2000)]


class RunClose(DTO):
    state: Literal["succeeded", "failed", "interrupted", "unknown"]
    reason: Annotated[str | None, Field(max_length=2000)] = None


class ResolveRead(DTO):
    action: Literal["exclude_unresolved_read"]
    call_ids: Annotated[list[Identifier], Field(max_length=100)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    expected_revision: Revision


class SourceImport(DTO):
    kind: Literal["user_text", "existing_ref", "artifact", "legacy_message"]
    expected_revision: Revision
    label: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    text: Annotated[str | None, Field(min_length=1, max_length=20000)] = None
    ref_ids: Annotated[list[Identifier] | None, Field(min_length=1, max_length=8)] = None
    artifact_id: Identifier | None = None
    message_ids: Annotated[list[Identifier] | None, Field(min_length=1, max_length=20)] = None

    @model_validator(mode="after")
    def variant(self):
        expected = {
            "user_text": {"label", "text"}, "existing_ref": {"ref_ids"},
            "artifact": {"artifact_id"}, "legacy_message": {"message_ids"},
        }[self.kind]
        actual = {field for field in ("label", "text", "ref_ids", "artifact_id", "message_ids") if getattr(self, field) is not None}
        if expected != actual:
            raise ValueError("Supply only the fields for the selected import kind")
        return self


class EvidenceProposal(DTO):
    ref_ids: Annotated[list[Identifier], Field(min_length=1, max_length=8)]
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    interpretation: Annotated[str | None, Field(min_length=1, max_length=2000)] = None

    @field_validator("ref_ids")
    @classmethod
    def no_duplicates(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Duplicate source references are not allowed")
        return value


class EvidenceUpdate(EvidenceProposal):
    expected_current_version: Revision
    expected_revision: Revision


class EvidenceReview(DTO):
    evidence_version: Revision
    decision: Literal["adopted", "needs_review", "excluded"]
    reason: Annotated[str | None, Field(min_length=1, max_length=2000)] = None
    expected_revision: Revision

    @model_validator(mode="after")
    def reason_required(self):
        if self.decision != "adopted" and not self.reason:
            raise ValueError("A reason is required for exclusion or review")
        return self


class EvidenceChoice(DTO):
    evidence_id: Identifier
    version: Revision


class ResultAccept(DTO):
    expected_revision: Revision
    evidence_versions: Annotated[list[EvidenceChoice], Field(min_length=1, max_length=100)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    narrative: Annotated[str | None, Field(min_length=1, max_length=10000)] = None

    @field_validator("evidence_versions")
    @classmethod
    def unique_evidence(cls, value):
        ids = [entry.evidence_id for entry in value]
        if len(set(ids)) != len(ids):
            raise ValueError("A snapshot can contain each evidence only once")
        return value


class CaseComplete(DTO):
    snapshot_id: Identifier
    expected_revision: Revision


class CaseTransition(DTO):
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    expected_revision: Revision


class CaseTrash(DTO):
    expected_revision: Revision


def iso(value):
    return value.isoformat() if value is not None else None


def case_data(case):
    return {
        "id": case.id, "owner_id": case.owner_id, "team_id": case.team_id,
        "instance_id": case.instance_id, "chat_id": case.chat_id,
        "title": case.title, "purpose": case.purpose, "profile": case.profile,
        "lifecycle": case.lifecycle, "revision": case.revision,
        "active_run_id": case.active_run_id, "deleted_at": iso(case.deleted_at),
        "created_at": iso(case.created_at), "updated_at": iso(case.updated_at),
    }


def ref_data(ref):
    return {
        "id": ref.id, "case_id": ref.case_id, "run_id": ref.run_id, "call_id": ref.call_id,
        "provenance_status": ref.provenance_status, "label": ref.label,
        "locator": ref.locator, "content": ref.content, "content_hash": ref.content_hash,
        "shareable": ref.shareable, "created_at": iso(ref.created_at),
    }


def evidence_data(evidence, version):
    return {
        "id": evidence.id, "evidence_id": evidence.id, "case_id": evidence.case_id,
        "current_version": evidence.current_version, "version": version.version,
        "summary": version.summary, "interpretation": version.interpretation,
        "ref_ids": version.ref_ids, "metrics": version.metrics,
        "review_status": version.review_status, "review_reason": version.review_reason,
        "reviewed_by": version.reviewed_by, "reviewed_at": iso(version.reviewed_at),
        "created_at": iso(version.created_at),
    }


def snapshot_data(snapshot):
    return {
        "id": snapshot.id, "case_id": snapshot.case_id, "title": snapshot.title,
        "narrative": snapshot.narrative, "profile": snapshot.profile,
        "body": snapshot.body, "content_hash": snapshot.content_hash, "state": snapshot.state,
        "created_by": snapshot.created_by, "created_at": iso(snapshot.created_at),
    }


def report_data(report):
    return {"id": report.id, "case_id": report.case_id, "snapshot_id": report.snapshot_id,
            "format": report.format, "template_id": report.template_id,
            "template_version": report.template_version, "state": report.state,
            "artifact_id": report.artifact_id, "outdated": report.outdated,
            "error_code": report.error_code, "created_at": iso(report.created_at)}


def call_data(call):
    return {
        "id": call.id, "call_id": call.id, "run_id": call.run_id,
        "invocation_id": call.invocation_id, "tool_id": call.tool_id,
        "tool_version": call.tool_version, "arguments": call.arguments,
        "state": call.state, "result_ref_ids": call.result_ref_ids,
        "upstream_request_id": call.upstream_request_id,
        "error_code": call.error_code, "disposition": call.disposition,
        "created_at": iso(call.created_at), "finished_at": iso(call.finished_at),
    }


def run_data(run):
    return {"id": run.id, "case_id": run.case_id, "request_id": run.request_id,
            "user_message": run.user_message, "input_ref_ids": run.input_ref_ids,
            "state": run.state, "trace_complete": run.trace_complete,
            "profile": run.profile, "model_id": run.model_id,
            "created_at": iso(run.created_at), "finished_at": iso(run.finished_at)}


def current_evidence(db, evidence):
    return db.scalar(select(EvidenceVersion).where(
        EvidenceVersion.evidence_id == evidence.id,
        EvidenceVersion.version == evidence.current_version,
    ))


def get_ref(db, ref_id, p, case_id=None, seen=None):
    """Recheck an imported reference's origin ACL instead of broadening access."""
    ref = db.get(SourceRef, ref_id)
    if not ref or (case_id is not None and ref.case_id != case_id):
        raise APIError(404, "NOT_FOUND", "Source reference is unavailable")
    source_case = db.get(Case, ref.case_id)
    if not source_case or source_case.deleted_at or source_case.instance_id != p.instance_id or not (
        source_case.owner_id == p.user_id or (source_case.team_id and source_case.team_id in p.team_ids)
    ):
        raise APIError(404, "NOT_FOUND", "Source reference is unavailable")
    # Context scope controls the destination; imported origins retain independent ACL checks.
    chain = seen or set()
    if ref.id in chain or len(chain) >= 32:
        raise APIError(422, "SOURCE_INVALID", "Source import chain is invalid")
    chain.add(ref.id)
    origin = (ref.locator or {}).get("imported_from_ref_id")
    if origin:
        get_ref(db, origin, p, seen=chain)
    if ref.call_id:
        call = db.get(ToolCall, ref.call_id)
        if not call or call.disposition == "excluded_by_human":
            raise APIError(422, "SOURCE_INVALID", "Excluded or unavailable calls cannot supply evidence")
    return ref


def visible_refs(db, case, p):
    result = []
    for ref in db.scalars(select(SourceRef).where(SourceRef.case_id == case.id)):
        try:
            get_ref(db, ref.id, p, case.id)
        except APIError:
            continue
        result.append(ref_data(ref))
    return result


def checked_refs(db, ids, p, case_id):
    if len(ids) != len(set(ids)):
        raise APIError(422, "SOURCE_INVALID", "Duplicate references")
    return [get_ref(db, item, p, case_id) for item in ids]


def assert_no_run(db, case):
    if case.active_run_id:
        raise APIError(409, "RUN_ACTIVE", "Resolve or close the active run first")
    unresolved = db.scalar(select(ToolCall.id).join(Run, ToolCall.run_id == Run.id).where(
        Run.case_id == case.id,
        ToolCall.state.in_(["claimed", "unknown"]),
        (ToolCall.disposition.is_(None)) | (ToolCall.disposition != "excluded_by_human"),
    ).limit(1))
    if unresolved:
        raise APIError(409, "TRACE_INCOMPLETE", "Unresolved source calls require review")


def require_trace_context(p, case=None, run=None):
    require_kind(p, "trace_adapter")
    if not p.chat_id or not p.case_id:
        raise APIError(403, "FORBIDDEN", "Recorder requires signed case and chat context")
    if case and (p.case_id != case.id or p.chat_id != case.chat_id or p.instance_id != case.instance_id):
        raise APIError(404, "NOT_FOUND", "Case is unavailable")
    if run and p.run_id != run.id:
        raise APIError(403, "FORBIDDEN", "Recorder requires the exact run scope")


def require_model_context(p, case, refs=None, db=None):
    if p.kind == "model_delegate":
        if p.case_id != case.id or not p.run_id:
            raise APIError(403, "FORBIDDEN", "Model requires exact case and run scope")
        if db is not None:
            scoped_run = db.get(Run, p.run_id)
            if not scoped_run or scoped_run.case_id != case.id:
                raise APIError(403, "FORBIDDEN", "Delegated run is not a recorded case run")
        if refs and any(ref.run_id not in (None, p.run_id) for ref in refs):
            raise APIError(403, "FORBIDDEN", "Reference is outside the delegated run")


def _page(query, db, limit, cursor):
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            if len(decoded) > 80:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise APIError(422, "SCHEMA_INVALID", "Invalid cursor")
        entity = query.column_descriptions[0]["entity"]
        query = query.where(entity.id > decoded)
    entity = query.column_descriptions[0]["entity"]
    rows = list(db.scalars(query.order_by(entity.id).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = base64.urlsafe_b64encode(rows[-1].id.encode()).decode() if more else None
    return rows, next_cursor


@router.get("/profiles")
def profiles(p: Actor):
    require_kind(p, "human", "model_delegate", "trace_adapter")
    return ok({"items": [DEFAULT_PROFILE]})


@router.get("/context/current")
def current_context(p: Actor, db: DB):
    require_kind(p, "human", "trace_adapter", "model_delegate")
    if not p.chat_id:
        raise APIError(403, "FORBIDDEN", "Signed chat context is required")
    case = db.scalar(select(Case).where(Case.instance_id == p.instance_id,
                                       Case.chat_id == p.chat_id, Case.deleted_at.is_(None)))
    if not case:
        return ok({"case": None, "run_id": None})
    case_for(db, case.id, p)
    latest = db.scalar(select(Run).where(Run.case_id == case.id).order_by(Run.created_at.desc()).limit(1))
    return ok({"case": case_data(case), "run_id": case.active_run_id or (latest.id if latest else None)})


@router.post("/cases")
def create_case(body: CaseCreate, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    if not p.chat_id:
        raise APIError(403, "FORBIDDEN", "Case creation requires signed chat context")
    if (body.profile_id, body.profile_version) != ("quality-general", 1):
        raise APIError(422, "PROFILE_UNSUPPORTED", "Only quality-general version 1 is implemented")
    # Serialize absent-row creation as well as updates across PostgreSQL workers.
    if db.bind.dialect.name == "postgresql":
        lock_id = int(digest([p.instance_id, p.chat_id])[:15], 16)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})

    def perform():
        existing = db.scalar(select(Case).where(Case.instance_id == p.instance_id, Case.chat_id == p.chat_id))
        if existing:
            case_for(db, existing.id, p)
            return case_data(existing)
        case = Case(id=new_id("case"), owner_id=p.user_id, instance_id=p.instance_id,
                    chat_id=p.chat_id, title=body.title, purpose=body.purpose,
                    profile=dict(DEFAULT_PROFILE), lifecycle="active", revision=1)
        db.add(case)
        db.flush()
        audit(db, p, "case.created", case.id, case.id)
        return case_data(case)
    return ok(idempotent(db, p, "case.create", p.chat_id, key, body.model_dump(), perform))


@router.get("/cases")
def list_cases(p: Actor, db: DB, chat_id: str | None = None,
               limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None):
    require_kind(p, "human")
    query = select(Case).where(Case.deleted_at.is_(None), Case.instance_id == p.instance_id,
                               (Case.owner_id == p.user_id) | Case.team_id.in_(p.team_ids))
    if p.case_id:
        query = query.where(Case.id == p.case_id)
    if p.chat_id:
        query = query.where(Case.chat_id == p.chat_id)
    if chat_id:
        query = query.where(Case.chat_id == chat_id)
    rows, cursor = _page(query, db, limit, cursor)
    return ok({"items": [case_data(row) for row in rows], "next_cursor": cursor})


@router.get("/cases/{case_id}")
def get_case(case_id: str, p: Actor, db: DB):
    require_kind(p, "human", "model_delegate", "trace_adapter")
    case = case_for(db, case_id, p)
    require_model_context(p, case, db=db)
    data = case_data(case)
    data["refs"] = visible_refs(db, case, p)
    allowed_ids = {ref["id"] for ref in data["refs"]}
    data["evidence"] = []
    for evidence in db.scalars(select(Evidence).where(Evidence.case_id == case.id)):
        version = current_evidence(db, evidence)
        if version and set(version.ref_ids) <= allowed_ids:
            data["evidence"].append(evidence_data(evidence, version))
    data["snapshots"] = []
    for snap in db.scalars(select(Snapshot).where(Snapshot.case_id == case.id)):
        ids = {ref["id"] for ev in snap.body.get("evidence_versions", []) for ref in ev.get("refs", [])}
        if ids <= allowed_ids:
            data["snapshots"].append(snapshot_data(snap))
    data["reports"] = [report_data(row) for row in db.scalars(select(Report).where(Report.case_id == case.id))]
    data["runs"] = [run_data(row) for row in db.scalars(select(Run).where(Run.case_id == case.id))]
    return ok(data)


@router.post("/cases/{case_id}/runs")
def start_run(case_id: str, body: RunStart, p: Actor, db: DB, key: Key):
    require_kind(p, "trace_adapter")
    case = case_for(db, case_id, p, lock=True)
    require_trace_context(p, case)

    def perform():
        previous = db.scalar(select(Run).where(Run.case_id == case.id, Run.request_id == body.request_id))
        input_hash = digest({"user_message": body.user_message, "input_ref_ids": body.input_ref_ids})
        if previous:
            if previous.input_hash != input_hash:
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "Request ID was already used with different input")
            return run_data(previous)
        assert_active(case)
        assert_revision(case, body.expected_revision)
        assert_no_run(db, case)
        checked_refs(db, body.input_ref_ids, p, case.id)
        run = Run(id=new_id("run"), case_id=case.id, request_id=body.request_id,
                  input_hash=input_hash, user_message=body.user_message,
                  input_ref_ids=body.input_ref_ids, state="running", trace_complete=False,
                  profile=dict(case.profile))
        db.add(run)
        db.flush()
        case.active_run_id = run.id
        bump(case)
        audit(db, p, "run.started", run.id, case.id)
        return run_data(run) | {"case_revision": case.revision}
    return ok(idempotent(db, p, "run.start", case.id, key, body.model_dump(), perform))


@router.get("/runs/{run_id}")
def get_run(run_id: str, p: Actor, db: DB):
    require_kind(p, "human", "model_delegate", "trace_adapter")
    run, case = run_for(db, run_id, p)
    if p.kind == "trace_adapter":
        require_trace_context(p, case, run)
    require_model_context(p, case, db=db)
    result = run_data(run)
    result["calls"] = [call_data(row) for row in db.scalars(select(ToolCall).where(ToolCall.run_id == run.id))]
    result["refs"] = [ref for ref in visible_refs(db, case, p) if ref["run_id"] == run.id]
    return ok(result)


@router.post("/runs/{run_id}/calls/claim")
def claim_call(run_id: str, body: CallClaim, request: Request, p: Actor, db: DB, key: Key):
    require_kind(p, "trace_adapter")
    run, case = run_for(db, run_id, p, lock=True)
    require_trace_context(p, case, run)
    token = None

    def perform():
        nonlocal token
        previous = db.scalar(select(ToolCall).where(ToolCall.run_id == run.id, ToolCall.invocation_id == body.invocation_id))
        args_hash = digest(body.arguments)
        if previous:
            if (previous.tool_id, previous.tool_version, previous.args_hash) != (body.tool_id, body.tool_version, args_hash):
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "Invocation ID already binds different arguments")
            return {"call_id": previous.id}
        assert_active(case)
        if run.state != "running" or case.active_run_id != run.id:
            raise APIError(409, "INVALID_STATE", "Run is not active")
        registered = request.app.state.settings.tool_registry.get(body.tool_id)
        if not registered:
            raise APIError(503, "NOT_CONFIGURED", "Tool is not in the managed registry")
        if registered.get("effect") != "read":
            raise APIError(403, "EFFECT_BLOCKED", "Managed source execution permits read tools only")
        if str(registered.get("version")) != body.tool_version:
            raise APIError(422, "TOOL_VERSION_MISMATCH", "Tool version is not registered")
        try:
            Draft202012Validator(registered["input_schema"]).validate(body.arguments)
            encoded_args = json.dumps(body.arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded_args) > 65536:
                raise ValueError()
        except (JSONValidationError, ValueError, TypeError):
            raise APIError(422, "SCHEMA_INVALID", "Arguments do not match the registered tool schema")
        token = secrets.token_urlsafe(32)
        call = ToolCall(id=new_id("call"), run_id=run.id, invocation_id=body.invocation_id,
                        tool_id=body.tool_id, tool_version=body.tool_version,
                        arguments=body.arguments, args_hash=args_hash, state="claimed",
                        claim_token_hash=digest(token), adapter_subject=p.subject,
                        epoch=p.epoch, result_ref_ids=[])
        db.add(call)
        db.flush()
        audit(db, p, "call.claimed", call.id, case.id, {"tool_id": call.tool_id})
        # Cache only the identifier. Execution authority is never persisted in a replay response.
        return {"call_id": call.id}

    saved = idempotent(db, p, "call.claim", run.id, key, body.model_dump(), perform)
    call = db.get(ToolCall, saved["call_id"])
    return ok(call_data(call) | {"execute": token is not None, "claim_token": token})


@router.post("/calls/{call_id}/outcome")
def record_outcome(call_id: str, body: CallOutcome, request: Request, p: Actor, db: DB, key: Key):
    require_kind(p, "trace_adapter")
    call = db.get(ToolCall, call_id)
    if not call:
        raise APIError(404, "NOT_FOUND", "Call not found")
    run, case = run_for(db, call.run_id, p, lock=True)
    db.refresh(call)
    require_trace_context(p, case, run)
    if call.adapter_subject != p.subject or call.epoch != p.epoch or not secrets.compare_digest(call.claim_token_hash, digest(body.claim_token)):
        raise APIError(403, "FORBIDDEN", "Recorder does not own this claim")
    outcome_hash = digest(body.model_dump(exclude={"claim_token"}))
    if call.outcome_hash:
        if call.outcome_hash != outcome_hash:
            raise APIError(409, "IDEMPOTENCY_CONFLICT", "Stored call outcomes are immutable")
        return ok(call_data(call) | {"refs": [ref_data(db.get(SourceRef, ref_id)) for ref_id in call.result_ref_ids]})
    if call.state not in {"claimed", "unknown"}:
        raise APIError(409, "INVALID_STATE", "Call cannot accept an outcome")
    # Late outcomes are evidence records only; they never reopen a run or alter snapshots.
    late = run.state != "running" or case.lifecycle != "active"
    refs = []
    if body.result is not None:
        registration = request.app.state.settings.tool_registry.get(call.tool_id, {})
        ref = SourceRef(id=new_id("ref"), case_id=case.id, run_id=run.id, call_id=call.id,
                        provenance_status="observed", label=call.tool_id,
                        locator={"kind": "json_pointer", "pointer": "", "tool_version": call.tool_version},
                        content=body.result, content_hash=digest(body.result),
                        shareable=bool(registration.get("shareable", False)))
        db.add(ref)
        db.flush()
        refs = [ref]
    call.state = body.state
    call.outcome_hash = outcome_hash
    call.result_ref_ids = [ref.id for ref in refs]
    call.upstream_request_id = body.upstream_request_id
    call.error_code = body.error_code
    call.finished_at = utcnow()
    bump(case)
    audit(db, p, "call.late_outcome" if late else "call.outcome", call.id, case.id,
          {"state": body.state, "result_ref_ids": call.result_ref_ids})
    return ok(call_data(call) | {"refs": [ref_data(ref) for ref in refs]})


@router.post("/calls/{call_id}/reconciliation")
def reconcile_outcome(call_id: str, body: CallReconciliation, request: Request, p: Actor, db: DB, key: Key):
    """Record recovered results with fresh service auth and the original claim proof.

    A recovery epoch change revokes execution, not the ability to record independently
    recovered output. This endpoint never grants execute=true or calls a source.
    """
    require_kind(p, "trace_adapter")
    call = db.get(ToolCall, call_id)
    if not call:
        raise APIError(404, "NOT_FOUND", "Call not found")
    run, case = run_for(db, call.run_id, p, lock=True)
    db.refresh(call)
    require_trace_context(p, case, run)
    if call.adapter_subject != p.subject or not secrets.compare_digest(call.claim_token_hash, digest(body.claim_token)):
        raise APIError(403, "FORBIDDEN", "Original recorder claim proof is required")

    def perform():
        if call.state not in {"claimed", "unknown"} or body.state == "unknown":
            raise APIError(409, "INVALID_STATE", "Only unresolved calls can be reconciled with a known result")
        if call.upstream_request_id and call.upstream_request_id != body.upstream_request_id:
            raise APIError(409, "SOURCE_INVALID", "Upstream request identity does not match")
        previous = {"state": call.state, "outcome_hash": call.outcome_hash,
                    "ref_ids": call.result_ref_ids, "epoch": call.epoch}
        refs = []
        if body.result is not None:
            registration = request.app.state.settings.tool_registry.get(call.tool_id, {})
            ref = SourceRef(id=new_id("ref"), case_id=case.id, run_id=run.id, call_id=call.id,
                            provenance_status="observed", label=call.tool_id,
                            locator={"kind": "json_pointer", "pointer": "", "tool_version": call.tool_version,
                                     "reconciled": True},
                            content=body.result, content_hash=digest(body.result),
                            shareable=bool(registration.get("shareable", False)))
            db.add(ref)
            db.flush()
            refs.append(ref)
        call.state = body.state
        call.outcome_hash = digest(body.model_dump(exclude={"claim_token", "reconciliation_reason"}))
        call.result_ref_ids = list(call.result_ref_ids) + [ref.id for ref in refs]
        call.upstream_request_id = body.upstream_request_id
        call.error_code = body.error_code
        call.finished_at = utcnow()
        # An exclusion remains excluded even if late source output becomes available.
        # Reuse requires explicit user import/review in a new workflow, never insertion
        # into an already accepted snapshot.
        bump(case)
        audit(db, p, "call.reconciled", call.id, case.id,
              {"previous": previous, "state": call.state, "new_ref_ids": [ref.id for ref in refs],
               "reason": body.reconciliation_reason})
        return call_data(call) | {"refs": [ref_data(ref) for ref in refs]}
    return ok(idempotent(db, p, "call.reconcile", call.id, key, body.model_dump(), perform))


@router.post("/runs/{run_id}/close")
def close_run(run_id: str, body: RunClose, p: Actor, db: DB, key: Key):
    require_kind(p, "trace_adapter")
    run, case = run_for(db, run_id, p, lock=True)
    require_trace_context(p, case, run)

    def perform():
        if run.state not in {"running", "unknown"}:
            raise APIError(409, "INVALID_STATE", "Run is already closed")
        calls = list(db.scalars(select(ToolCall).where(ToolCall.run_id == run.id)))
        unresolved = [call for call in calls if call.state in {"claimed", "unknown"} and call.disposition != "excluded_by_human"]
        if body.state != "unknown" and unresolved:
            raise APIError(409, "TRACE_INCOMPLETE", "Unresolved calls prevent a verified close")
        if body.state == "succeeded" and any(call.state != "succeeded" for call in calls):
            raise APIError(409, "INVALID_STATE", "Successful close requires successful calls")
        if body.state == "unknown":
            for call in calls:
                if call.state == "claimed":
                    call.state = "unknown"
            run.trace_complete = False
        else:
            run.trace_complete = not unresolved
            case.active_run_id = None
        run.state = body.state
        run.finished_at = utcnow()
        bump(case)
        audit(db, p, "run.closed", run.id, case.id, {"state": run.state, "reason": body.reason})
        return run_data(run) | {"case_revision": case.revision}
    return ok(idempotent(db, p, "run.close", run.id, key, body.model_dump(), perform))


@router.post("/runs/{run_id}/resolve-read")
def resolve_read(run_id: str, body: ResolveRead, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    run, case = run_for(db, run_id, p, lock=True)

    def perform():
        assert_active(case)
        assert_revision(case, body.expected_revision)
        if run.state not in {"running", "unknown", "interrupted"}:
            raise APIError(409, "INVALID_STATE", "This run has no unresolved read workflow")
        calls = list(db.scalars(select(ToolCall).where(ToolCall.run_id == run.id)))
        by_id = {call.id: call for call in calls}
        if not body.call_ids and any(call.state in {"claimed", "unknown"} and call.disposition != "excluded_by_human" for call in calls):
            raise APIError(422, "SOURCE_INVALID", "Select the unresolved calls explicitly")
        if len(body.call_ids) != len(set(body.call_ids)) or any(item not in by_id for item in body.call_ids):
            raise APIError(422, "SOURCE_INVALID", "Invalid call selection")
        for call_id in body.call_ids:
            call = by_id[call_id]
            if call.state not in {"claimed", "unknown"}:
                raise APIError(409, "INVALID_STATE", "Only unresolved read calls can be excluded")
            # Only read calls can be created through this API. Their actual outcome remains unknown.
            call.state = "unknown"
            call.disposition = "excluded_by_human"
            audit(db, p, "call.read_excluded", call.id, case.id, {"reason": body.reason})
        remaining = [call for call in calls if call.state in {"claimed", "unknown"} and call.disposition != "excluded_by_human"]
        if not remaining:
            run.state = "interrupted"
            run.trace_complete = False
            run.finished_at = utcnow()
            if case.active_run_id == run.id:
                case.active_run_id = None
        bump(case)
        return run_data(run) | {"case_revision": case.revision}
    return ok(idempotent(db, p, "run.resolve_read", run.id, key, body.model_dump(), perform))


@router.post("/cases/{case_id}/source-imports")
def import_source(case_id: str, body: SourceImport, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    case = case_for(db, case_id, p, lock=True)
    if body.kind == "existing_ref":
        for ref_id in body.ref_ids:
            get_ref(db, ref_id, p)

    def perform():
        assert_active(case)
        assert_revision(case, body.expected_revision)
        if body.kind in {"artifact", "legacy_message"}:
            raise APIError(503, "NOT_CONFIGURED", "Verified upload and legacy-chat adapters are not configured")
        refs = []
        if body.kind == "user_text":
            ref = SourceRef(id=new_id("ref"), case_id=case.id, provenance_status="user_provided",
                            label=body.label, locator={"kind": "user_text"}, content=body.text,
                            content_hash=digest(body.text), shareable=False)
            db.add(ref)
            refs.append(ref)
        else:
            if len(body.ref_ids) != len(set(body.ref_ids)):
                raise APIError(422, "SOURCE_INVALID", "Duplicate references")
            originals = [get_ref(db, ref_id, p) for ref_id in body.ref_ids]
            for original in originals:
                ref = SourceRef(id=new_id("ref"), case_id=case.id,
                                provenance_status=original.provenance_status, label=original.label,
                                locator={"kind": "import", "imported_from_ref_id": original.id},
                                content=original.content, content_hash=original.content_hash,
                                shareable=original.shareable)
                db.add(ref)
                refs.append(ref)
        db.flush()
        bump(case)
        audit(db, p, "source.imported", case.id, case.id, {"kind": body.kind, "ref_ids": [ref.id for ref in refs]})
        return {"refs": [ref_data(ref) for ref in refs], "case_revision": case.revision}
    return ok(idempotent(db, p, "source.import", case.id, key, body.model_dump(), perform))


@router.post("/cases/{case_id}/evidence/proposals")
def propose_evidence(case_id: str, body: EvidenceProposal, p: Actor, db: DB, key: Key):
    require_kind(p, "human", "model_delegate")
    case = case_for(db, case_id, p, lock=True)
    refs = checked_refs(db, body.ref_ids, p, case.id)
    require_model_context(p, case, refs, db=db)

    def perform():
        assert_active(case)
        refs = checked_refs(db, body.ref_ids, p, case.id)
        require_model_context(p, case, refs, db=db)
        evidence = Evidence(id=new_id("ev"), case_id=case.id, current_version=1)
        db.add(evidence)
        db.flush()
        version = EvidenceVersion(id=new_id("evv"), evidence_id=evidence.id, version=1,
                                  summary=body.summary, interpretation=body.interpretation,
                                  ref_ids=body.ref_ids, metrics={}, review_status="unreviewed")
        db.add(version)
        db.flush()
        bump(case)
        audit(db, p, "evidence.proposed", evidence.id, case.id, {"version": 1})
        return evidence_data(evidence, version) | {"case_revision": case.revision}
    return ok(idempotent(db, p, "evidence.propose", case.id, key, body.model_dump(), perform))


def evidence_for(db, evidence_id, p):
    evidence = db.get(Evidence, evidence_id)
    if not evidence:
        raise APIError(404, "NOT_FOUND", "Evidence not found")
    case = case_for(db, evidence.case_id, p, lock=True)
    db.refresh(evidence)
    return evidence, case


@router.post("/evidence/{evidence_id}/versions")
def new_evidence_version(evidence_id: str, body: EvidenceUpdate, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    evidence, case = evidence_for(db, evidence_id, p)
    checked_refs(db, body.ref_ids, p, case.id)

    def perform():
        assert_active(case)
        assert_revision(case, body.expected_revision)
        if evidence.current_version != body.expected_current_version:
            raise APIError(409, "REVISION_CONFLICT", "Evidence version has changed")
        checked_refs(db, body.ref_ids, p, case.id)
        evidence.current_version += 1
        version = EvidenceVersion(id=new_id("evv"), evidence_id=evidence.id, version=evidence.current_version,
                                  summary=body.summary, interpretation=body.interpretation,
                                  ref_ids=body.ref_ids, metrics={}, review_status="unreviewed")
        db.add(version)
        db.flush()
        invalidate(db, case, evidence.id)
        bump(case)
        audit(db, p, "evidence.version_created", evidence.id, case.id, {"version": version.version})
        return evidence_data(evidence, version) | {"case_revision": case.revision}
    return ok(idempotent(db, p, "evidence.version", evidence.id, key, body.model_dump(), perform))


@router.post("/evidence/{evidence_id}/reviews")
def review_evidence(evidence_id: str, body: EvidenceReview, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    evidence, case = evidence_for(db, evidence_id, p)
    requested = db.scalar(select(EvidenceVersion).where(EvidenceVersion.evidence_id == evidence.id,
                                                       EvidenceVersion.version == body.evidence_version))
    if requested:
        checked_refs(db, requested.ref_ids, p, case.id)

    def perform():
        assert_active(case)
        assert_revision(case, body.expected_revision)
        if evidence.current_version != body.evidence_version:
            raise APIError(409, "REVISION_CONFLICT", "Review the current evidence version")
        version = current_evidence(db, evidence)
        checked_refs(db, version.ref_ids, p, case.id)
        if body.decision != version.review_status:
            invalidate(db, case, evidence.id)
        version.review_status = body.decision
        version.review_reason = body.reason
        version.reviewed_by = p.user_id
        version.reviewed_at = utcnow()
        bump(case)
        audit(db, p, "evidence.reviewed", evidence.id, case.id,
              {"version": version.version, "decision": body.decision, "reason": body.reason})
        return evidence_data(evidence, version) | {"case_revision": case.revision}
    return ok(idempotent(db, p, "evidence.review", evidence.id, key, body.model_dump(), perform))


@router.post("/cases/{case_id}/results")
def accept_result(case_id: str, body: ResultAccept, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    case = case_for(db, case_id, p, lock=True)
    for chosen in body.evidence_versions:
        candidate = db.get(Evidence, chosen.evidence_id)
        if not candidate or candidate.case_id != case.id:
            raise APIError(404, "NOT_FOUND", "Evidence not found")
        selected_version = db.scalar(select(EvidenceVersion).where(
            EvidenceVersion.evidence_id == candidate.id, EvidenceVersion.version == chosen.version))
        if selected_version:
            checked_refs(db, selected_version.ref_ids, p, case.id)

    def perform():
        assert_active(case)
        assert_revision(case, body.expected_revision)
        assert_no_run(db, case)
        entries = []
        for chosen in body.evidence_versions:
            evidence = db.get(Evidence, chosen.evidence_id)
            if not evidence or evidence.case_id != case.id:
                raise APIError(404, "NOT_FOUND", "Evidence not found")
            version = current_evidence(db, evidence)
            if evidence.current_version != chosen.version or version.review_status != "adopted":
                raise APIError(409, "EVIDENCE_NOT_CURRENT_ADOPTED", "Results require the current adopted evidence versions")
            refs = checked_refs(db, version.ref_ids, p, case.id)
            entries.append(evidence_data(evidence, version) | {"refs": [ref_data(ref) for ref in refs]})
        content = {"evidence_versions": entries, "profile": case.profile,
                   "title": body.title, "narrative": body.narrative,
                   "accepted_by": p.user_id, "accepted_at": utcnow().isoformat()}
        if len(json.dumps(content, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8 * 1024 * 1024:
            raise APIError(422, "LIMIT_EXCEEDED", "Snapshot exceeds 8 MiB; select bounded source excerpts")
        snapshot = Snapshot(id=new_id("snap"), case_id=case.id, title=body.title,
                            narrative=body.narrative, profile=dict(case.profile), body=content,
                            content_hash=digest(content), state="accepted", created_by=p.user_id)
        db.add(snapshot)
        db.flush()
        bump(case)
        audit(db, p, "result.accepted", snapshot.id, case.id)
        return snapshot_data(snapshot) | {"case_revision": case.revision}
    return ok(idempotent(db, p, "result.accept", case.id, key, body.model_dump(), perform))


@router.post("/cases/{case_id}/complete")
def complete_case(case_id: str, body: CaseComplete, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    case = case_for(db, case_id, p, lock=True)

    def perform():
        assert_active(case)
        assert_revision(case, body.expected_revision)
        assert_no_run(db, case)
        snapshot = db.get(Snapshot, body.snapshot_id)
        if not snapshot or snapshot.case_id != case.id or snapshot.state != "accepted":
            raise APIError(409, "INVALID_STATE", "Completion requires a current accepted snapshot")
        for entry in snapshot.body["evidence_versions"]:
            evidence = db.get(Evidence, entry["evidence_id"])
            version = current_evidence(db, evidence)
            if evidence.current_version != entry["version"] or version.review_status != "adopted":
                raise APIError(409, "INVALID_STATE", "Snapshot evidence needs renewed review")
            checked_refs(db, version.ref_ids, p, case.id)
        case.lifecycle = "completed"
        bump(case)
        audit(db, p, "case.completed", case.id, case.id, {"snapshot_id": snapshot.id})
        return case_data(case)
    return ok(idempotent(db, p, "case.complete", case.id, key, body.model_dump(), perform))


def transition(case_id, body, p, db, key, action):
    require_kind(p, "human")
    case = case_for(db, case_id, p, lock=True)

    def perform():
        assert_revision(case, body.expected_revision)
        assert_no_run(db, case)
        if action == "reopen":
            if case.lifecycle not in {"completed", "archived"}:
                raise APIError(409, "INVALID_STATE", "Only completed or archived cases can reopen")
            case.lifecycle = "active"
        else:
            if case.lifecycle not in {"active", "completed"}:
                raise APIError(409, "INVALID_STATE", "Case is already archived")
            case.lifecycle = "archived"
        bump(case)
        audit(db, p, f"case.{action}", case.id, case.id, {"reason": body.reason})
        return case_data(case)
    return ok(idempotent(db, p, f"case.{action}", case.id, key, body.model_dump(), perform))


@router.post("/cases/{case_id}/reopen")
def reopen_case(case_id: str, body: CaseTransition, p: Actor, db: DB, key: Key):
    return transition(case_id, body, p, db, key, "reopen")


@router.post("/cases/{case_id}/archive")
def archive_case(case_id: str, body: CaseTransition, p: Actor, db: DB, key: Key):
    return transition(case_id, body, p, db, key, "archive")


@router.post("/cases/{case_id}/trash")
def trash_case(case_id: str, body: CaseTrash, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    case = case_for(db, case_id, p, lock=True, allow_deleted=True)

    def perform():
        assert_revision(case, body.expected_revision)
        assert_no_run(db, case)
        case.deleted_at = utcnow()
        bump(case)
        audit(db, p, "case.trashed", case.id, case.id)
        return case_data(case)
    return ok(idempotent(db, p, "case.trash", case.id, key, body.model_dump(), perform))


@router.post("/cases/{case_id}/restore")
def restore_case(case_id: str, body: CaseTrash, p: Actor, db: DB, key: Key):
    require_kind(p, "human")
    case = db.scalar(select(Case).where(Case.id == case_id).with_for_update())
    if not case or case.owner_id != p.user_id or case.instance_id != p.instance_id or (p.case_id and p.case_id != case.id):
        raise APIError(404, "NOT_FOUND", "Case not found")

    def perform():
        assert_revision(case, body.expected_revision)
        if not case.deleted_at:
            raise APIError(409, "INVALID_STATE", "Case is not trashed")
        case.deleted_at = None
        bump(case)
        audit(db, p, "case.restored", case.id, case.id)
        return case_data(case)
    return ok(idempotent(db, p, "case.restore", case.id, key, body.model_dump(), perform))


@router.get("/cases/{case_id}/audit")
def get_audit(case_id: str, p: Actor, db: DB,
              limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: str | None = None):
    require_kind(p, "human")
    case = case_for(db, case_id, p)
    if case.owner_id != p.user_id:
        raise APIError(403, "FORBIDDEN", "Audit history is owner-only")
    rows, cursor = _page(select(Audit).where(Audit.case_id == case.id), db, limit, cursor)
    return ok({"items": [{"id": row.id, "actor_id": row.actor_id, "actor_kind": row.actor_kind,
                          "action": row.action, "entity_id": row.entity_id, "details": row.details,
                          "created_at": iso(row.created_at)} for row in rows], "next_cursor": cursor})
