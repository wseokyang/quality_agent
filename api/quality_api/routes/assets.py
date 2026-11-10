"""Versioned, approval-gated quality knowledge and guided procedure library.

This module deliberately has no network execution or remote JSON-schema resolution.
Procedure structure validation never certifies real-tool execution.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, principal, require_kind
from ..common import audit, case_for, digest, idempotent, new_id, ok, utcnow
from ..db import get_db
from ..errors import APIError
from ..models import Asset, AssetVersion, Audit, Case, Feedback, Run, SourceRef, ToolCall

router = APIRouter()
SUPPORT = {
    "guided": True,
    "structural_validation": True,
    "automatic_execution": False,
    "execution_validation": False,
    "execution_unavailable_reason": "No approved procedure runner or registered real-tool validation fixtures are configured. Guided instructions require normal Tool permissions and human review.",
    "knowledge_distribution": "controlled_api_only",
}
# Supported schema vocabulary excludes remote refs, regex and executable validators.
_SCHEMA_KEYS = {
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "title", "description", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "minProperties",
    "maxProperties", "anyOf", "oneOf", "allOf", "format", "$schema",
}
_SENSITIVE = re.compile(r"(^|[_\-.])(password|passwd|secret|token|credential|authorization|cookie|api[_-]?key|private[_-]?key|command|shell|script)([_\-.]|$)", re.I)
_SECRET_VALUE = re.compile(r"-----BEGIN .*PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|\b(?:password|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*['\"]?[^\s'\"<>{}]{8,}", re.I)


def _sensitive(name):
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return _SENSITIVE.search(normalized)


def _parse_expiry(value):
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not (value.endswith("Z") or value.endswith("+00:00")):
        raise ValueError("Expiry must be an explicit UTC ISO timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ToolBinding(Strict):
    binding_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    tool_id: str = Field(min_length=1, max_length=200)
    tool_version: str = Field(min_length=1, max_length=100)


class InputBinding(Strict):
    target: str = Field(min_length=1, max_length=200)
    from_: Literal["parameter", "step_output", "literal"] = Field(alias="from")
    key: str | None = Field(default=None, max_length=200)
    step_id: str | None = Field(default=None, max_length=100)
    pointer: str | None = Field(default=None, max_length=1000)
    value: Any = None

    @model_validator(mode="after")
    def correct_shape(self):
        fields = self.model_fields_set
        if self.from_ == "parameter" and (not self.key or self.step_id is not None or self.pointer is not None or "value" in fields):
            raise ValueError("parameter bindings require only key")
        if self.from_ == "step_output" and (not self.step_id or self.pointer is None or self.key is not None or "value" in fields):
            raise ValueError("step_output bindings require step_id and pointer")
        if self.from_ == "literal" and ("value" not in fields or self.key is not None or self.step_id is not None or self.pointer is not None):
            raise ValueError("literal bindings require only value")
        return self


class ProcedureStep(Strict):
    step_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    binding_id: str = Field(min_length=1, max_length=100)
    depends_on: list[str] = Field(default_factory=list, max_length=30)
    input_bindings: list[InputBinding] = Field(default_factory=list, max_length=50)
    required_output_refs: list[str] = Field(default_factory=list, max_length=30)


class Procedure(Strict):
    mode: Literal["guided", "validated"] = "guided"
    parameter_schema: dict = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    tool_bindings: list[ToolBinding] = Field(default_factory=list, max_length=30)
    profile_id: str = Field(default="general", min_length=1, max_length=100)
    profile_version: str = Field(default="1", min_length=1, max_length=100)
    steps: list[ProcedureStep] = Field(default_factory=list, max_length=30)
    output_schema: dict = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    validation_fixture_refs: list[str] = Field(default_factory=list, max_length=30)


class AssetDraft(Strict):
    kind: Literal["procedure", "knowledge"]
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    content_md: str = Field(min_length=1, max_length=50000)
    ref_ids: list[str] = Field(min_length=1, max_length=20)
    category: str = Field(min_length=1, max_length=100)
    applicability: str = Field(min_length=1, max_length=2000)
    proposed_expires_at: datetime | None = None

    @field_validator("proposed_expires_at", mode="before")
    @classmethod
    def parse_expiry(cls, value):
        return _parse_expiry(value)

    procedure: Procedure | None = None

    @field_validator("title", "summary", "content_md", "category", "applicability")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def correct_kind(self):
        if (self.kind == "knowledge") != (self.procedure is None):
            raise ValueError("knowledge must not have a procedure; procedure assets must include one")
        if len(set(self.ref_ids)) != len(self.ref_ids):
            raise ValueError("ref_ids must be unique")
        return self


class AssetRevisionDraft(AssetDraft):
    expected_asset_revision: int = Field(ge=1)
    expected_current_version: int = Field(ge=1)


class AssetApproval(Strict):
    expected_asset_revision: int = Field(ge=1)
    asset_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime | None = None

    @field_validator("expires_at", mode="before")
    @classmethod
    def parse_expiry(cls, value):
        return _parse_expiry(value)


class AssetSubmission(Strict):
    expected_asset_revision: int = Field(ge=1)
    asset_version: int = Field(ge=1)
    target_team_id: str = Field(min_length=1, max_length=200)


class CurationDecision(Strict):
    expected_asset_revision: int = Field(ge=1)
    asset_version: int = Field(ge=1)
    action: Literal["publish", "reject", "propose_merge"]
    reason: str = Field(min_length=1, max_length=2000)
    merge_target_id: str | None = None


class AssetFeedback(Strict):
    asset_version: int = Field(ge=1)
    kind: Literal["useful", "incorrect"]
    comment: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def explain_error(self):
        if self.kind == "incorrect" and not (self.comment and self.comment.strip()):
            raise ValueError("incorrect feedback requires reproduction conditions or explanation")
        return self


class AssetValidation(Strict):
    expected_asset_revision: int = Field(ge=1)
    asset_version: int = Field(ge=1)
    fixture_ids: list[str] = Field(default_factory=list, max_length=30)


class ProcedureDraft(Strict):
    run_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    category: str = Field(default="quality", min_length=1, max_length=100)
    applicability: str = Field(min_length=1, max_length=2000)


class AssetLifecycle(Strict):
    expected_asset_revision: int = Field(ge=1)
    asset_version: int = Field(ge=1)
    action: Literal["deprecate", "archive"]
    reason: str = Field(min_length=1, max_length=2000)


class SourceVersion(Strict):
    asset_id: str
    version: int = Field(ge=1)


class SourceRevision(Strict):
    asset_id: str
    revision: int = Field(ge=1)


class MergeConfirmation(Strict):
    target_asset_id: str
    target_asset_version: int = Field(ge=1)
    source_asset_versions: list[SourceVersion] = Field(min_length=1, max_length=1)
    proposed_content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_target_revision: int = Field(ge=1)
    expected_source_revisions: list[SourceRevision] = Field(min_length=1, max_length=1)


def _settings(request):
    return request.app.state.settings


def _teams(p, settings):
    # Resolve memberships on every request, never trust team claims from caller.
    return set(settings.user_teams.get(p.user_id, []))


def _curator(p, settings):
    return p.kind == "human" and p.user_id in settings.curator_ids


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _iso(value):
    return _aware(value).isoformat() if value else None


def _expiry(value):
    if value is not None:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise APIError(422, "INVALID_EXPIRY", "expires_at must be an explicit UTC timestamp")
        if not utcnow() < value <= utcnow() + timedelta(days=365):
            raise APIError(422, "INVALID_EXPIRY", "expires_at must be in the future and at most 365 days ahead")
    return value


def _effective(asset):
    if asset.status in {"personal", "submitted", "published"} and asset.expires_at and _aware(asset.expires_at) <= utcnow():
        return "expired"
    return asset.status


def _case_readable(case, p, settings):
    return bool(case and case.instance_id == p.instance_id and not case.deleted_at and case.lifecycle not in {"trashed", "trash", "archived", "deleted"}
                and (case.owner_id == p.user_id or (case.team_id and case.team_id in _teams(p, settings))))


def _refs(db, ids, p, settings, *, case_id=None, sharing_team=None, sharing_owner_id=None, _chain=None):
    refs = []
    for ref_id in ids:
        ref = db.get(SourceRef, ref_id)
        if ref is None or (case_id is not None and ref.case_id != case_id):
            raise APIError(404, "SOURCE_NOT_FOUND", "A referenced source is not accessible")
        source_case = db.get(Case, ref.case_id)
        source_live = bool(source_case and source_case.instance_id == p.instance_id and not source_case.deleted_at
                           and source_case.lifecycle not in {"trashed", "trash", "archived", "deleted"})
        if not source_live:
            raise APIError(403, "SOURCE_ACCESS_DENIED", "Current source access is required")
        if sharing_team is None:
            if not _case_readable(source_case, p, settings):
                raise APIError(403, "SOURCE_ACCESS_DENIED", "Current source access is required")
        else:
            # A deliberate owner submission may share the derived asset without exposing
            # its whole parent case. The original sharing principal must retain access.
            sharing_owner = sharing_owner_id or p.user_id
            owner_has_access = source_case.owner_id == sharing_owner or (
                source_case.team_id and source_case.team_id in settings.user_teams.get(sharing_owner, []))
            if not ref.shareable or not owner_has_access:
                raise APIError(403, "SOURCE_NOT_SHAREABLE", "Every source must permit sharing and the asset owner must retain source access")
            if sharing_team not in _teams(p, settings) and not _case_readable(source_case, p, settings):
                raise APIError(403, "SOURCE_ACCESS_DENIED", "Current team membership is required for shared asset access")
        chain = set(_chain or ())
        if ref.id in chain or len(chain) >= 32:
            raise APIError(403, "SOURCE_ACCESS_DENIED", "Source import chain is invalid")
        chain.add(ref.id)
        origin = (ref.locator or {}).get("imported_from_ref_id")
        if origin:
            _refs(db, [origin], p, settings, sharing_team=sharing_team, sharing_owner_id=sharing_owner_id, _chain=chain)
        if ref.call_id:
            call = db.get(ToolCall, ref.call_id)
            if not call or call.disposition == "excluded_by_human":
                raise APIError(403, "SOURCE_ACCESS_DENIED", "An excluded or unavailable Tool call cannot supply reusable knowledge")
        refs.append(ref)
    return refs


def _asset(db, asset_id, p, settings, *, lock=False, review=False):
    require_kind(p, "human", "model_delegate")
    query = select(Asset).where(Asset.id == asset_id)
    if lock:
        query = query.with_for_update()
    asset = db.scalar(query)
    if asset is None:
        raise APIError(404, "ASSET_NOT_FOUND", "Asset is not accessible")
    version = db.scalar(select(AssetVersion).where(AssetVersion.asset_id == asset.id, AssetVersion.version == asset.current_version))
    if version is None:
        raise APIError(409, "ASSET_VERSION_MISSING", "Asset version is unavailable")
    effective = _effective(asset)
    owner_review = p.kind == "human" and asset.owner_id == p.user_id
    curator_review = _curator(p, settings) and asset.team_id in _teams(p, settings) and asset.status in {"submitted", "published", "expired", "deprecated", "archived"}
    reusable = effective in {"personal", "published"} and (
        asset.owner_id == p.user_id or (effective == "published" and asset.team_id in _teams(p, settings)))
    if not reusable and not (owner_review or curator_review):
        raise APIError(404, "ASSET_NOT_FOUND", "Asset is not accessible")
    if p.kind == "model_delegate" and not reusable:
        raise APIError(404, "ASSET_NOT_FOUND", "Only approved, nonexpired assets are reusable")
    _refs(db, version.ref_ids, p, settings, sharing_team=asset.team_id if asset.status in {"submitted", "published"} else None, sharing_owner_id=asset.owner_id)
    return asset, version


def _owner(asset, p):
    require_kind(p, "human")
    if asset.owner_id != p.user_id:
        raise APIError(403, "ASSET_OWNER_REQUIRED", "Only the asset owner can perform this action")


def _revision(asset, expected, version):
    if asset.revision != expected or asset.current_version != version:
        raise APIError(409, "ASSET_REVISION_CONFLICT", "The asset changed; reload before deciding", {"revision": asset.revision, "asset_version": asset.current_version})


def _touch(asset):
    asset.revision += 1
    asset.updated_at = utcnow()


def _version_data(version):
    return {
        "version": version.version, "title": version.title, "summary": version.summary,
        "content_md": version.content_md, "ref_ids": version.ref_ids, "category": version.category,
        "applicability": version.applicability, "procedure": version.procedure,
        "content_hash": version.content_hash, "validation_status": version.validation_status,
        "validation_report": version.validation_report, "created_at": _iso(version.created_at),
    }


def _data(asset, version, p, settings):
    owner = p.kind == "human" and p.user_id == asset.owner_id
    curator = _curator(p, settings) and asset.team_id in _teams(p, settings)
    effective = _effective(asset)
    return {
        "id": asset.id, "case_id": asset.case_id, "kind": asset.kind, "owner_id": asset.owner_id,
        "team_id": asset.team_id, "current_version": asset.current_version, "revision": asset.revision,
        "status": asset.status, "effective_status": effective, "expires_at": _iso(asset.expires_at),
        "created_at": _iso(asset.created_at), "updated_at": _iso(asset.updated_at),
        "usable": effective in {"personal", "published"}, "version": _version_data(version),
        "permissions": {"approve": owner and asset.status == "draft", "submit": owner and effective == "personal",
                        "curate": curator and effective == "submitted", "revise": owner and asset.status != "archived",
                        "validate": owner and asset.status == "draft", "feedback": p.kind == "human" and effective in {"personal", "published"},
                        "deprecate": (owner or curator) and effective in {"personal", "submitted", "published", "expired"},
                        "archive": (owner or curator) and effective in {"expired", "deprecated"},
                        "suggest_merge": owner or curator},
        "support": SUPPORT,
    }


def _historical_version_access(db, asset_id, version_number, p, settings, expected_hash=None):
    """A current version's ACL never authorizes cached text from an older version."""
    asset, _ = _asset(db, asset_id, p, settings)
    version = db.scalar(select(AssetVersion).where(AssetVersion.asset_id == asset_id, AssetVersion.version == version_number))
    if version is None or (expected_hash is not None and version.content_hash != expected_hash):
        raise APIError(403, "SOURCE_ACCESS_DENIED", "The original response version is no longer accessible")
    # An owner needs direct access to the old sources. A team reviewer additionally
    # needs the present sharing grant and shareability of every original source.
    sharing_team = asset.team_id if asset.owner_id != p.user_id else None
    _refs(db, version.ref_ids, p, settings, sharing_team=sharing_team, sharing_owner_id=asset.owner_id)


def _check_cached_asset_response(db, payload, p, settings):
    if not isinstance(payload, dict):
        return
    version = payload.get("version")
    if isinstance(payload.get("id"), str) and isinstance(version, dict) and isinstance(version.get("version"), int):
        _historical_version_access(db, payload["id"], version["version"], p, settings, version.get("content_hash"))
    if isinstance(payload.get("asset_id"), str) and isinstance(payload.get("asset_version"), int):
        _historical_version_access(db, payload["asset_id"], payload["asset_version"], p, settings)
    if isinstance(payload.get("target_asset_id"), str) and isinstance(payload.get("target_asset_version"), int):
        _historical_version_access(db, payload["target_asset_id"], payload["target_asset_version"], p, settings)
    for source in payload.get("source_asset_versions", []):
        _historical_version_access(db, source["asset_id"], source["version"], p, settings)
    # Recurse only through server-defined result containers. Procedure schemas and
    # other user-authored JSON are data, not instructions to resolve extra IDs.
    for name in ("asset", "target", "source", "proposal"):
        if name in payload:
            _check_cached_asset_response(db, payload[name], p, settings)


def _asset_idempotent(db, p, settings, operation, entity, key, payload, fn):
    executed = False
    def fresh():
        nonlocal executed
        executed = True
        return fn()
    result = idempotent(db, p, operation, entity, key, payload, fresh)
    if not executed:
        if isinstance(payload.get("asset_version"), int):
            _historical_version_access(db, entity, payload["asset_version"], p, settings)
        _check_cached_asset_response(db, result, p, settings)
    return result


def _safe_schema(schema, path="schema", depth=0):
    if depth > 12 or not isinstance(schema, dict):
        raise APIError(422, "UNSUPPORTED_PROCEDURE_SCHEMA", f"{path}: only bounded object schemas are supported")
    if len(json.dumps(schema, ensure_ascii=False)) > 30000:
        raise APIError(422, "UNSUPPORTED_PROCEDURE_SCHEMA", "Schema is too large")
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            raise APIError(422, "UNSUPPORTED_PROCEDURE_SCHEMA", f"{path}: unsupported schema keyword {key}")
        if key == "$schema" and value != "https://json-schema.org/draft/2020-12/schema":
            raise APIError(422, "UNSUPPORTED_PROCEDURE_SCHEMA", "Only local Draft 2020-12 validation is supported")
        if key == "properties":
            if not isinstance(value, dict) or len(value) > 100:
                raise APIError(422, "UNSUPPORTED_PROCEDURE_SCHEMA", "properties must be a bounded object")
            for name, item in value.items():
                if _sensitive(name):
                    raise APIError(422, "UNSAFE_PROCEDURE_FIELD", f"Sensitive or executable parameter is forbidden: {name}")
                _safe_schema(item, path + "." + name, depth + 1)
        if key in {"items", "additionalProperties"} and isinstance(value, dict):
            _safe_schema(value, path + "." + key, depth + 1)
        if key in {"anyOf", "allOf", "oneOf"}:
            if not isinstance(value, list) or len(value) > 20:
                raise APIError(422, "UNSUPPORTED_PROCEDURE_SCHEMA", "Schema alternatives exceed the supported bound")
            for item in value:
                _safe_schema(item, path + "." + key, depth + 1)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise APIError(422, "INVALID_PROCEDURE_SCHEMA", "Procedure contains an invalid JSON Schema") from exc


def _check_procedure(procedure, settings):
    if procedure is None:
        return
    data = procedure.model_dump(by_alias=True, exclude_none=True) if isinstance(procedure, Procedure) else procedure
    _safe_schema(data["parameter_schema"], "parameter_schema")
    _safe_schema(data["output_schema"], "output_schema")
    if data["parameter_schema"].get("type") != "object" or data["output_schema"].get("type") != "object":
        raise APIError(422, "INVALID_PROCEDURE_SCHEMA", "Input and output schemas must have type object")
    if data.get("validation_fixture_refs"):
        raise APIError(422, "VALIDATION_FIXTURES_UNAVAILABLE", "No registered execution-validation fixture store is configured")
    bindings = {}
    for binding in data["tool_bindings"]:
        if binding["binding_id"] in bindings:
            raise APIError(422, "DUPLICATE_BINDING", "Tool binding IDs must be unique")
        registry = settings.tool_registry.get(binding["tool_id"])
        if not registry or registry["version"] != binding["tool_version"] or registry.get("effect") != "read":
            raise APIError(422, "UNREGISTERED_PROCEDURE_TOOL", "Procedure bindings must use currently registered read-only Tool versions")
        bindings[binding["binding_id"]] = registry
    steps = {step["step_id"]: step for step in data["steps"]}
    if len(steps) != len(data["steps"]):
        raise APIError(422, "DUPLICATE_STEP", "Procedure step IDs must be unique")
    if data["mode"] == "validated" and not steps:
        raise APIError(422, "PROCEDURE_STEPS_REQUIRED", "An executable procedure proposal requires explicit steps")
    done = set()
    for step in data["steps"]:
        if step["binding_id"] not in bindings:
            raise APIError(422, "UNKNOWN_BINDING", "Step references an unknown Tool binding")
        if any(dep not in done for dep in step["depends_on"]):
            raise APIError(422, "INVALID_STEP_ORDER", "Steps must be ordered; dependencies must precede the current step and cannot form a cycle")
        inputs = bindings[step["binding_id"]].get("input_schema", {}).get("properties", {})
        mapped = set()
        for mapping in step["input_bindings"]:
            target = mapping["target"]
            if target not in inputs or target in mapped or _sensitive(target):
                raise APIError(422, "UNSAFE_INPUT_BINDING", "Bindings must target unique registered nonsensitive Tool fields")
            mapped.add(target)
            kind = mapping["from"]
            if kind == "parameter" and mapping.get("key") not in data["parameter_schema"].get("properties", {}):
                raise APIError(422, "UNKNOWN_PARAMETER", "Input binding references an unknown procedure parameter")
            if kind == "step_output":
                # Registry currently specifies input schemas only, so output pointer existence cannot be proven.
                raise APIError(422, "STEP_OUTPUT_SCHEMA_UNAVAILABLE", "Step-output mappings require registered Tool output schemas, which this deployment does not yet provide")
            if kind == "literal":
                encoded = json.dumps(mapping.get("value"), ensure_ascii=False)
                if "http://" in encoded or "https://" in encoded or _SECRET_VALUE.search(encoded):
                    raise APIError(422, "UNSAFE_LITERAL", "Procedure literals must not include arbitrary URLs or credentials")
                if not Draft202012Validator(inputs[target]).is_valid(mapping.get("value")):
                    raise APIError(422, "INVALID_LITERAL", "Literal does not satisfy the registered Tool input schema")
        required = set(bindings[step["binding_id"]].get("input_schema", {}).get("required", []))
        if not required.issubset(mapped):
            raise APIError(422, "MISSING_REQUIRED_BINDING", "All required Tool fields must have an explicit parameter mapping")
        done.add(step["step_id"])


def _content(draft):
    return {"kind": draft.kind, "title": draft.title, "summary": draft.summary, "content_md": draft.content_md,
            "ref_ids": draft.ref_ids, "category": draft.category, "applicability": draft.applicability,
            "procedure": draft.procedure.model_dump(by_alias=True, exclude_none=True) if draft.procedure else None}


def _create_version(db, asset, draft, number):
    content = _content(draft)
    version = AssetVersion(id=new_id("av"), asset_id=asset.id, version=number,
                           title=draft.title, summary=draft.summary, content_md=draft.content_md,
                           ref_ids=list(draft.ref_ids), category=draft.category, applicability=draft.applicability,
                           procedure=content["procedure"], content_hash=digest(content),
                           validation_status="unvalidated", validation_report={}, created_at=utcnow())
    db.add(version)
    db.flush()
    return version


def _check_draft(draft, db, p, settings, case_id=None):
    if _SECRET_VALUE.search("\n".join([draft.title, draft.summary, draft.content_md, draft.category, draft.applicability])):
        raise APIError(422, "POSSIBLE_CREDENTIAL", "Remove credential-like content before creating a reusable asset")
    _expiry(draft.proposed_expires_at)
    _refs(db, draft.ref_ids, p, settings, case_id=case_id)
    _check_procedure(draft.procedure, settings)


def _create_draft(db, case, draft, p, settings):
    _check_draft(draft, db, p, settings, case_id=case.id)
    now = utcnow()
    asset = Asset(id=new_id("ast"), case_id=case.id, kind=draft.kind, owner_id=p.user_id, team_id=None,
                  current_version=1, revision=1, status="draft", expires_at=draft.proposed_expires_at,
                  created_at=now, updated_at=now)
    db.add(asset)
    db.flush()
    version = _create_version(db, asset, draft, 1)
    audit(db, p, "asset.draft_created", asset.id, case_id=case.id, details={"version": 1, "content_hash": version.content_hash, "kind": draft.kind})
    return _data(asset, version, p, settings)


@router.post("/cases/{case_id}/asset-drafts")
def create_asset_draft(case_id: str, body: AssetDraft, request: Request, p: Principal = Depends(principal),
                       db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human", "model_delegate")
    case = case_for(db, case_id, p)
    _check_draft(body, db, p, _settings(request), case_id=case.id)
    return ok(idempotent(db, p, "asset.draft", case_id, key, body.model_dump(mode="json"),
                         lambda: _create_draft(db, case, body, p, _settings(request))))


def _strip_schema(schema):
    """Drop examples/defaults and unsupported vocabulary; never copy invocation values."""
    if not isinstance(schema, dict):
        return {"type": "string"}
    clean = {}
    for key, value in schema.items():
        if key in {"default", "examples", "title", "description", "$schema"}:
            continue
        if key not in _SCHEMA_KEYS:
            raise APIError(422, "UNSUPPORTED_TOOL_SCHEMA", "Recorded Tool schema uses unsupported constraints; author a reviewed guided procedure manually")
        if key == "properties":
            clean[key] = {name: _strip_schema(item) for name, item in value.items() if not _sensitive(name)}
        elif key in {"items", "additionalProperties"} and isinstance(value, dict):
            clean[key] = _strip_schema(value)
        elif key in {"anyOf", "oneOf", "allOf"}:
            clean[key] = [_strip_schema(item) for item in value]
        elif key not in {"description", "title"}:
            clean[key] = value
    return clean or {"type": "string"}


@router.post("/cases/{case_id}/procedure-drafts")
def procedure_from_run(case_id: str, body: ProcedureDraft, request: Request, p: Principal = Depends(principal),
                       db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human", "model_delegate")
    case = case_for(db, case_id, p)
    settings = _settings(request)

    def perform():
        run = db.get(Run, body.run_id)
        if not run or run.case_id != case.id:
            raise APIError(404, "RUN_NOT_FOUND", "Recorded run is not accessible")
        if run.state not in {"completed", "succeeded"} or not run.trace_complete:
            raise APIError(409, "RUN_NOT_COMPLETE", "Procedure extraction requires a closed run with complete execution records")
        calls = list(db.scalars(select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.created_at, ToolCall.id)))
        if not calls or len(calls) > 30:
            raise APIError(422, "UNSUPPORTED_RUN_SIZE", "A guided draft requires between 1 and 30 recorded successful Tool calls")
        params, bindings, steps, refs, lines = {}, [], [], [], []
        required_params = []
        for index, call in enumerate(calls, 1):
            registry = settings.tool_registry.get(call.tool_id)
            if call.state not in {"succeeded", "completed", "success"} or not registry or registry.get("effect") != "read" or registry["version"] != call.tool_version:
                raise APIError(409, "UNSUPPORTED_RECORDED_CALL", "Every recorded call must be successful and use a current registered read-only Tool version")
            if any(_sensitive(name) for name in call.arguments):
                raise APIError(422, "SENSITIVE_RECORDED_INPUT", "Recorded invocation includes sensitive or executable input fields; create a reviewed draft manually")
            step_id = f"step_{index}"
            binding_id = f"tool_{index}"
            bindings.append({"binding_id": binding_id, "tool_id": call.tool_id, "tool_version": call.tool_version})
            mappings = []
            schema = registry.get("input_schema", {})
            properties = schema.get("properties", {})
            for name in call.arguments:
                if name not in properties:
                    raise APIError(422, "UNDECLARED_RECORDED_INPUT", "Recorded arguments must have explicit registered property schemas")
                parameter = f"{step_id}__{name}"
                params[parameter] = _strip_schema(properties[name])
                required_params.append(parameter)
                mappings.append({"target": name, "from": "parameter", "key": parameter})
            steps.append({"step_id": step_id, "binding_id": binding_id,
                          "depends_on": [f"step_{index-1}"] if index > 1 else [],
                          "input_bindings": mappings, "required_output_refs": []})
            refs.extend(call.result_ref_ids or [])
            lines.append(f"{index}. 등록 Tool `{call.tool_id}` 버전 `{call.tool_version}`의 입력을 확인하고 조회합니다. 결과와 출처를 검토한 뒤 다음 단계로 진행합니다.")
        refs = list(dict.fromkeys(refs))
        if not refs or len(refs) > 20:
            raise APIError(422, "UNSUPPORTED_SOURCE_COUNT", "A draft must have 1–20 recorded result references")
        procedure = Procedure.model_validate({"mode": "guided", "parameter_schema": {"type": "object", "properties": params, "required": required_params, "additionalProperties": False},
                                              "tool_bindings": bindings, "profile_id": run.profile.get("profile_id", run.profile.get("id", "general")),
                                              "profile_version": str(run.profile.get("profile_version", run.profile.get("version", "1"))), "steps": steps,
                                              "output_schema": {"type": "object", "properties": {}, "additionalProperties": False}, "validation_fixture_refs": []})
        markdown = f"# {body.title}\n\n{body.summary}\n\n## 적용 조건\n\n{body.applicability}\n\n## 입력\n\n원래 실행의 입력 값은 복사하지 않았습니다. 실행할 건에 맞는 파라미터를 사용자가 입력하고 확인해야 합니다.\n\n## 작업 순서\n\n" + "\n".join(lines) + "\n\n## 검토\n\n각 결과의 출처·완전성·적용 조건을 확인합니다. 이 문서는 지침형 초안이며 자동 실행이나 실행 검증을 보장하지 않습니다.\n"
        draft = AssetDraft(kind="procedure", title=body.title, summary=body.summary, content_md=markdown, ref_ids=refs,
                           category=body.category, applicability=body.applicability, procedure=procedure)
        result = _create_draft(db, case, draft, p, settings)
        audit(db, p, "procedure.extracted", result["id"], case_id=case.id, details={"source_run_id": run.id, "invocation_count": len(calls), "argument_values_copied": False})
        return result

    source_run = db.get(Run, body.run_id)
    if not source_run or source_run.case_id != case.id:
        raise APIError(404, "RUN_NOT_FOUND", "Recorded run is not accessible")
    replay_ref_ids = [ref for call in db.scalars(select(ToolCall).where(ToolCall.run_id == source_run.id)) for ref in (call.result_ref_ids or [])]
    _refs(db, replay_ref_ids, p, settings, case_id=case.id)
    return ok(idempotent(db, p, "procedure.draft", case_id, key, body.model_dump(mode="json"), perform))


@router.get("/assets")
def list_assets(request: Request, kind: Literal["procedure", "knowledge"] | None = None,
                status: str | None = None, case_id: str | None = None, q: str | None = Query(default=None, max_length=200),
                include_expired: bool = False, limit: int = Query(default=50, ge=1, le=100),
                p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require_kind(p, "human", "model_delegate")
    settings = _settings(request)
    query = select(Asset).order_by(Asset.updated_at.desc(), Asset.id)
    if kind:
        query = query.where(Asset.kind == kind)
    if case_id:
        query = query.where(Asset.case_id == case_id)
    candidates = db.scalars(query).all()
    result = []
    for candidate in candidates:
        effective = _effective(candidate)
        if effective in {"expired", "deprecated", "archived"} and not (include_expired and p.kind == "human"):
            continue
        if status and effective != status:
            continue
        try:
            asset, version = _asset(db, candidate.id, p, settings)
        except APIError as exc:
            if getattr(exc, "status", getattr(exc, "status_code", None)) in {403, 404}:
                continue
            # Error objects from the application always include a code; access errors must not leak.
            if getattr(exc, "code", None) in {"ASSET_NOT_FOUND", "SOURCE_NOT_FOUND", "SOURCE_ACCESS_DENIED", "SOURCE_NOT_SHAREABLE"}:
                continue
            raise
        if q and q.casefold() not in (version.title + " " + version.summary + " " + version.category + " " + version.content_md).casefold():
            continue
        result.append(_data(asset, version, p, settings))
        if len(result) >= limit:
            break
    return ok({"items": result, "support": SUPPORT})


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, request: Request, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    asset, version = _asset(db, asset_id, p, _settings(request))
    result = _data(asset, version, p, _settings(request))
    if p.kind == "human":
        result["history"] = [{"version": row.version, "content_hash": row.content_hash, "created_at": _iso(row.created_at)} for row in db.scalars(select(AssetVersion).where(AssetVersion.asset_id == asset.id).order_by(AssetVersion.version))]
        result["feedback"] = [{"id": row.id, "asset_version": row.asset_version, "kind": row.kind, "comment": row.comment, "created_at": _iso(row.created_at)} for row in db.scalars(select(Feedback).where(Feedback.asset_id == asset.id, Feedback.asset_version == asset.current_version).order_by(Feedback.created_at.desc()).limit(50))]
    return ok(result)


@router.post("/assets/{asset_id}/versions")
def revise_asset(asset_id: str, body: AssetRevisionDraft, request: Request, p: Principal = Depends(principal),
                 db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        asset, old = _asset(db, asset_id, p, settings, lock=True)
        _owner(asset, p)
        _revision(asset, body.expected_asset_revision, body.expected_current_version)
        if asset.status == "archived":
            raise APIError(409, "ASSET_ARCHIVED", "Create a new asset instead of revising an archived asset")
        if body.kind != asset.kind:
            raise APIError(422, "ASSET_KIND_IMMUTABLE", "Asset kind cannot change across versions")
        # A merge may legitimately contain source refs from several accessible cases.
        _check_draft(body, db, p, settings)
        version = _create_version(db, asset, body, asset.current_version + 1)
        asset.current_version = version.version
        asset.status = "draft"
        asset.team_id = None
        asset.expires_at = body.proposed_expires_at
        _touch(asset)
        audit(db, p, "asset.version_created", asset.id, case_id=asset.case_id,
              details={"previous_version": old.version, "version": version.version, "content_hash": version.content_hash})
        return _data(asset, version, p, settings)
    _asset(db, asset_id, p, settings)
    _check_draft(body, db, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.revise", asset_id, key, body.model_dump(mode="json"), perform))


@router.post("/assets/{asset_id}/approve")
def approve_asset(asset_id: str, body: AssetApproval, request: Request, p: Principal = Depends(principal),
                  db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        asset, version = _asset(db, asset_id, p, settings, lock=True)
        _owner(asset, p)
        _revision(asset, body.expected_asset_revision, body.asset_version)
        if asset.status != "draft":
            raise APIError(409, "INVALID_ASSET_STATE", "Only a draft can be approved for the personal library")
        if body.content_hash != version.content_hash:
            raise APIError(409, "ASSET_CONTENT_CONFLICT", "Approval must match the reviewed content hash")
        _check_procedure(version.procedure, settings)
        if version.procedure and version.procedure["mode"] == "validated":
            raise APIError(409, "EXECUTION_VALIDATION_UNAVAILABLE", "Executable procedure approval requires real execution evidence; revise to guided mode for human-reviewed reuse")
        asset.expires_at = _expiry(body.expires_at)
        asset.status = "personal"
        _touch(asset)
        audit(db, p, "asset.approved", asset.id, case_id=asset.case_id,
              details={"version": version.version, "content_hash": version.content_hash, "expires_at": _iso(asset.expires_at)})
        return _data(asset, version, p, settings)
    _asset(db, asset_id, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.approve", asset_id, key, body.model_dump(mode="json"), perform))


@router.post("/assets/{asset_id}/submit")
def submit_asset(asset_id: str, body: AssetSubmission, request: Request, p: Principal = Depends(principal),
                 db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        asset, version = _asset(db, asset_id, p, settings, lock=True)
        _owner(asset, p)
        _revision(asset, body.expected_asset_revision, body.asset_version)
        if _effective(asset) != "personal":
            raise APIError(409, "INVALID_ASSET_STATE", "Only an approved, unexpired personal asset can be submitted")
        if body.target_team_id not in _teams(p, settings):
            raise APIError(403, "TEAM_ACCESS_DENIED", "Target team is not an active user membership")
        _refs(db, version.ref_ids, p, settings, sharing_team=body.target_team_id)
        asset.team_id = body.target_team_id
        asset.status = "submitted"
        _touch(asset)
        audit(db, p, "asset.submitted", asset.id, case_id=asset.case_id,
              details={"version": version.version, "content_hash": version.content_hash, "team_id": asset.team_id})
        return _data(asset, version, p, settings)
    _asset(db, asset_id, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.submit", asset_id, key, body.model_dump(mode="json"), perform))


@router.post("/assets/{asset_id}/validate")
def validate_asset(asset_id: str, body: AssetValidation, request: Request, p: Principal = Depends(principal),
                   db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        asset, version = _asset(db, asset_id, p, settings, lock=True)
        _owner(asset, p)
        _revision(asset, body.expected_asset_revision, body.asset_version)
        if asset.status != "draft":
            raise APIError(409, "INVALID_ASSET_STATE", "Structural validation applies to a draft")
        if body.fixture_ids:
            raise APIError(422, "VALIDATION_FIXTURES_UNAVAILABLE", "No registered real-tool fixture runner is configured; fixture IDs cannot be accepted")
        _check_procedure(version.procedure, settings)
        version.validation_status = "structure_checked"
        version.validation_report = {"kind": "structure_only", "structure": "passed", "execution": "not_performed",
                                     "real_integration": "not_performed", "validated": False,
                                     "checked_at": _iso(utcnow()), "content_hash": version.content_hash,
                                     "reason": SUPPORT["execution_unavailable_reason"]}
        _touch(asset)
        audit(db, p, "asset.structure_checked", asset.id, case_id=asset.case_id,
              details={"version": version.version, "content_hash": version.content_hash, "execution_validated": False})
        return _data(asset, version, p, settings)
    _asset(db, asset_id, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.validate", asset_id, key, body.model_dump(mode="json"), perform))


def _require_curator(asset, p, settings):
    require_kind(p, "human")
    if not _curator(p, settings) or asset.team_id not in _teams(p, settings):
        raise APIError(403, "CURATOR_REQUIRED", "A configured curator for this team must review the decision")


def _merge_content(source, sv, target, tv):
    if source.kind != "knowledge" or target.kind != "knowledge":
        raise APIError(422, "PROCEDURE_MERGE_UNSUPPORTED", "Procedure steps require explicit author review; automatic merge proposals support knowledge only")
    refs = list(dict.fromkeys(tv.ref_ids + sv.ref_ids))
    if len(refs) > 20:
        raise APIError(422, "MERGE_SOURCE_LIMIT", "Merge would exceed the 20-source limit; curate the source set first")
    content = tv.content_md + "\n\n---\n\n## 병합 검토: " + sv.title + "\n\n" + sv.content_md
    if len(content) > 50000:
        raise APIError(422, "MERGE_CONTENT_LIMIT", "Merge would exceed the content size limit")
    return {"kind": "knowledge", "title": tv.title, "summary": tv.summary, "content_md": content, "ref_ids": refs,
            "category": tv.category, "applicability": tv.applicability, "procedure": None}


@router.post("/assets/{asset_id}/curation-decisions")
def curate_asset(asset_id: str, body: CurationDecision, request: Request, p: Principal = Depends(principal),
                 db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        # Fixed lock order also applies when proposing a merge.
        ids = sorted(set([asset_id] + ([body.merge_target_id] if body.merge_target_id else [])))
        pairs = {item: _asset(db, item, p, settings, lock=True) for item in ids}
        asset, version = pairs[asset_id]
        _require_curator(asset, p, settings)
        _revision(asset, body.expected_asset_revision, body.asset_version)
        if _effective(asset) != "submitted":
            raise APIError(409, "INVALID_ASSET_STATE", "Only an unexpired submitted asset can be curated")
        if not body.reason.strip():
            raise APIError(422, "CURATION_REASON_REQUIRED", "Record the human review reason")
        if body.action != "propose_merge" and body.merge_target_id is not None:
            raise APIError(422, "UNEXPECTED_MERGE_TARGET", "merge_target_id is only accepted for propose_merge")
        if body.action == "propose_merge":
            if not body.merge_target_id or body.merge_target_id == asset.id:
                raise APIError(422, "MERGE_TARGET_REQUIRED", "Select a different accessible knowledge asset")
            target, tv = pairs[body.merge_target_id]
            _require_curator(target, p, settings)
            if target.team_id != asset.team_id or _effective(target) != "published":
                raise APIError(409, "INVALID_MERGE_TARGET", "Merge target must be a published asset in the same team")
            content = _merge_content(asset, version, target, tv)
            _refs(db, content["ref_ids"], p, settings, sharing_team=asset.team_id, sharing_owner_id=target.owner_id)
            proposal = {"target_asset_id": target.id, "target_asset_version": tv.version,
                        "source_asset_versions": [{"asset_id": asset.id, "version": version.version}],
                        "proposed_content_hash": digest(content), "expected_target_revision": target.revision,
                        "expected_source_revisions": [{"asset_id": asset.id, "revision": asset.revision}],
                        "content": content, "reason": body.reason,
                        "notice": "Confirmation creates a new draft target version and deprecates the source. The target owner must approve and resubmit the merged version before publication."}
            # Audit is readable independently of the proposed target's source ACL.
            # Persist only the confirmation contract, never copied source prose.
            audit_proposal = {name: proposal[name] for name in (
                "target_asset_id", "target_asset_version", "source_asset_versions", "proposed_content_hash",
                "expected_target_revision", "expected_source_revisions", "reason")}
            audit(db, p, "asset.merge_proposed", asset.id, case_id=asset.case_id, details=audit_proposal)
            return {"proposal": proposal, "asset": _data(asset, version, p, settings)}
        if body.action == "publish":
            _refs(db, version.ref_ids, p, settings, sharing_team=asset.team_id, sharing_owner_id=asset.owner_id)
            _check_procedure(version.procedure, settings)
            if version.procedure and version.procedure["mode"] == "validated":
                raise APIError(409, "EXECUTION_VALIDATION_UNAVAILABLE", "Unvalidated executable procedures cannot be published")
            asset.status = "published"
        else:
            asset.status = "personal"
            asset.team_id = None
        _touch(asset)
        audit(db, p, "asset." + ("published" if body.action == "publish" else "rejected"), asset.id, case_id=asset.case_id,
              details={"version": version.version, "content_hash": version.content_hash, "reason": body.reason})
        return _data(asset, version, p, settings)
    _asset(db, asset_id, p, settings)
    if body.merge_target_id:
        _asset(db, body.merge_target_id, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.curate", asset_id, key, body.model_dump(mode="json"), perform))


@router.post("/assets/{asset_id}/merge-confirmations")
def confirm_merge(asset_id: str, body: MergeConfirmation, request: Request, p: Principal = Depends(principal),
                  db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        if body.source_asset_versions[0].asset_id != asset_id or body.expected_source_revisions[0].asset_id != asset_id or body.target_asset_id == asset_id:
            raise APIError(422, "MERGE_SOURCE_MISMATCH", "Confirmation must match the reviewed source asset")
        pairs = {item: _asset(db, item, p, settings, lock=True) for item in sorted([asset_id, body.target_asset_id])}
        source, sv = pairs[asset_id]
        target, tv = pairs[body.target_asset_id]
        _require_curator(source, p, settings)
        _require_curator(target, p, settings)
        _revision(source, body.expected_source_revisions[0].revision, body.source_asset_versions[0].version)
        _revision(target, body.expected_target_revision, body.target_asset_version)
        if source.team_id != target.team_id or _effective(source) != "submitted" or _effective(target) != "published":
            raise APIError(409, "MERGE_STATE_CHANGED", "Merge requires a submitted source and published target in the same team")
        content = _merge_content(source, sv, target, tv)
        if digest(content) != body.proposed_content_hash:
            raise APIError(409, "MERGE_CONTENT_CHANGED", "The reviewed merge content no longer matches")
        records = db.scalars(select(Audit).where(Audit.entity_id == source.id, Audit.action == "asset.merge_proposed").order_by(Audit.created_at.desc()).limit(50))
        expected = body.model_dump(mode="json")
        proposal = next((row.details for row in records if all(row.details.get(name) == value for name, value in expected.items())), None)
        if proposal is None:
            raise APIError(409, "MERGE_REVIEW_REQUIRED", "Generate and review this exact merge proposal before confirming")
        _refs(db, content["ref_ids"], p, settings, sharing_team=source.team_id, sharing_owner_id=target.owner_id)
        expiries = [_aware(x) for x in [source.expires_at, target.expires_at] if x is not None]
        expiry = min(expiries) if expiries else None
        draft = AssetDraft.model_validate({**content, "proposed_expires_at": expiry})
        version = _create_version(db, target, draft, target.current_version + 1)
        target.current_version = version.version
        target.status = "draft"
        target.team_id = None
        target.expires_at = expiry
        source.status = "deprecated"
        _touch(source)
        _touch(target)
        audit(db, p, "asset.merge_confirmed", target.id, case_id=target.case_id,
              details={"source_asset_id": source.id, "source_version": sv.version, "previous_target_version": tv.version,
                       "new_version": version.version, "content_hash": version.content_hash, "reason": proposal["reason"]})
        return {"target": _data(target, version, p, settings), "source": _data(source, sv, p, settings)}
    _asset(db, asset_id, p, settings)
    _asset(db, body.target_asset_id, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.merge", asset_id, key, body.model_dump(mode="json"), perform))


@router.get("/assets/{asset_id}/curation-suggestions")
def curation_suggestions(asset_id: str, request: Request, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    require_kind(p, "human")
    settings = _settings(request)
    asset, version = _asset(db, asset_id, p, settings)
    if asset.owner_id != p.user_id:
        _require_curator(asset, p, settings)
    words = set(re.findall(r"\w+", (version.title + " " + version.summary).casefold()))
    matches = []
    candidates = db.scalars(select(Asset).where(Asset.id != asset.id, Asset.kind == asset.kind).order_by(Asset.updated_at.desc()).limit(200))
    for candidate in candidates:
        try:
            other, ov = _asset(db, candidate.id, p, settings)
        except APIError:
            continue
        if _effective(other) not in {"personal", "published"}:
            continue
        other_words = set(re.findall(r"\w+", (ov.title + " " + ov.summary).casefold()))
        score = len(words & other_words) / max(len(words | other_words), 1)
        if score >= 0.15:
            matches.append({"asset_id": other.id, "asset_version": ov.version, "title": ov.title, "score": round(score, 3),
                            "same_category": ov.category == version.category, "method": "token_jaccard", "decision": "human_review_required"})
    matches.sort(key=lambda item: (-item["score"], item["asset_id"]))
    return ok({"items": matches[:5], "conflict_detection": "not_automated", "notice": "Text similarity suggests candidates only; it cannot establish factual equivalence or detect contradictions."})


@router.post("/assets/{asset_id}/feedback")
def feedback_asset(asset_id: str, body: AssetFeedback, request: Request, p: Principal = Depends(principal),
                   db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        asset, version = _asset(db, asset_id, p, settings)
        if _effective(asset) not in {"personal", "published"} or body.asset_version != asset.current_version:
            raise APIError(409, "FEEDBACK_VERSION_CONFLICT", "Feedback must reference the current approved version")
        record = Feedback(id=new_id("fb"), asset_id=asset.id, asset_version=body.asset_version, user_id=p.user_id,
                          kind=body.kind, comment=body.comment, created_at=utcnow())
        db.add(record)
        audit(db, p, "asset.feedback", asset.id, case_id=asset.case_id,
              details={"feedback_id": record.id, "version": body.asset_version, "kind": body.kind})
        return {"id": record.id, "asset_id": asset.id, "asset_version": body.asset_version, "kind": body.kind,
                "comment": body.comment, "created_at": _iso(record.created_at)}
    _asset(db, asset_id, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.feedback", asset_id, key, body.model_dump(mode="json"), perform))


@router.post("/assets/{asset_id}/lifecycle")
def lifecycle_asset(asset_id: str, body: AssetLifecycle, request: Request, p: Principal = Depends(principal),
                    db: Session = Depends(get_db), key: str | None = Header(default=None, alias="Idempotency-Key")):
    require_kind(p, "human")
    settings = _settings(request)
    def perform():
        asset, version = _asset(db, asset_id, p, settings, lock=True)
        if asset.owner_id != p.user_id:
            _require_curator(asset, p, settings)
        _revision(asset, body.expected_asset_revision, body.asset_version)
        if not body.reason.strip():
            raise APIError(422, "LIFECYCLE_REASON_REQUIRED", "Record the withdrawal reason")
        effective = _effective(asset)
        if body.action == "deprecate":
            if effective not in {"personal", "submitted", "published", "expired"}:
                raise APIError(409, "INVALID_ASSET_STATE", "Only an approved or expired asset can be deprecated")
            asset.status = "deprecated"
        else:
            if effective not in {"expired", "deprecated"}:
                raise APIError(409, "INVALID_ASSET_STATE", "Deprecate an asset before archiving it")
            asset.status = "archived"
        _touch(asset)
        audit(db, p, "asset." + asset.status, asset.id, case_id=asset.case_id, details={"version": version.version, "reason": body.reason})
        return _data(asset, version, p, settings)
    _asset(db, asset_id, p, settings)
    return ok(_asset_idempotent(db, p, settings, "asset.lifecycle", asset_id, key, body.model_dump(mode="json"), perform))


@router.get("/assets/{asset_id}/export")
def export_asset(asset_id: str, request: Request, p: Principal = Depends(principal), db: Session = Depends(get_db)):
    asset, version = _asset(db, asset_id, p, _settings(request))
    if _effective(asset) not in {"personal", "published"}:
        raise APIError(409, "ASSET_NOT_APPROVED", "Only an approved nonexpired asset can be exported for guided reuse")
    if version.procedure:
        _check_procedure(version.procedure, _settings(request))
    source_refs = _refs(db, version.ref_ids, p, _settings(request), sharing_team=asset.team_id if asset.status == "published" else None, sharing_owner_id=asset.owner_id)
    manifest = {
        "schema_version": 1, "asset_id": asset.id, "asset_version": version.version, "content_hash": version.content_hash,
        "kind": asset.kind, "title": version.title, "summary": version.summary, "status_at_export": asset.status,
        "expires_at": _iso(asset.expires_at), "validation_status": version.validation_status,
        "procedure": version.procedure, "allowed_effects": ["read"],
        "source_refs": [{"ref_id": ref.id, "content_hash": ref.content_hash, "provenance_status": ref.provenance_status} for ref in source_refs],
        "source_run_ids": sorted({ref.run_id for ref in source_refs if ref.run_id}),
        "support": SUPPORT,
        "authorization": "This snapshot does not grant access or certify execution. Recheck current asset status, expiry and source access through the quality API before each reuse. Do not copy into unrestricted Knowledge.",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if asset.kind == "procedure":
            # JSON string quoting is also valid YAML, preventing front-matter injection.
            front = "---\nname: " + json.dumps(asset.id, ensure_ascii=False) + "\ndescription: " + json.dumps(version.summary, ensure_ascii=False) + "\n---\n\n"
            bundle.writestr("SKILL.md", front + version.content_md)
        else:
            bundle.writestr("KNOWLEDGE.md", version.content_md)
        bundle.writestr("README.txt", "승인된 시점의 검토용 사본입니다. 재사용 전 API에서 현재 상태·만료·출처 권한을 확인하십시오. 지침은 실행 권한이나 검증 실행 인증이 아닙니다. 원천 데이터·실행 인자·인증정보는 이 패키지에 포함하지 않습니다.\n")
    audit(db, p, "asset.exported", asset.id, case_id=asset.case_id, details={"version": version.version, "content_hash": version.content_hash})
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{asset.id}-v{version.version}.zip"', "Cache-Control": "no-store"})
