from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "qa_cases"
    __table_args__ = (UniqueConstraint("instance_id", "chat_id"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    team_id: Mapped[str | None] = mapped_column(String(200))
    instance_id: Mapped[str] = mapped_column(String(200))
    chat_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(Text)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    lifecycle: Mapped[str] = mapped_column(String(30), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active_run_id: Mapped[str | None] = mapped_column(String(80))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = "qa_runs"
    __table_args__ = (UniqueConstraint("case_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(200))
    input_hash: Mapped[str] = mapped_column(String(64))
    user_message: Mapped[str] = mapped_column(Text)
    input_ref_ids: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(30), default="running")
    trace_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    model_id: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolCall(Base):
    __tablename__ = "qa_tool_calls"
    __table_args__ = (UniqueConstraint("run_id", "invocation_id"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id"), index=True)
    invocation_id: Mapped[str] = mapped_column(String(200))
    tool_id: Mapped[str] = mapped_column(String(200))
    tool_version: Mapped[str] = mapped_column(String(120))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    args_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="claimed")
    claim_token_hash: Mapped[str] = mapped_column(String(64))
    adapter_subject: Mapped[str] = mapped_column(String(200))
    epoch: Mapped[str] = mapped_column(String(80))
    outcome_hash: Mapped[str | None] = mapped_column(String(64))
    result_ref_ids: Mapped[list] = mapped_column(JSON, default=list)
    upstream_request_id: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(100))
    disposition: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceRef(Base):
    __tablename__ = "qa_source_refs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("qa_runs.id"))
    call_id: Mapped[str | None] = mapped_column(ForeignKey("qa_tool_calls.id"))
    provenance_status: Mapped[str] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(200))
    locator: Mapped[dict] = mapped_column(JSON, default=dict)
    content: Mapped[object] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    shareable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Evidence(Base):
    __tablename__ = "qa_evidence"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EvidenceVersion(Base):
    __tablename__ = "qa_evidence_versions"
    __table_args__ = (UniqueConstraint("evidence_id", "version"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("qa_evidence.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text)
    interpretation: Mapped[str | None] = mapped_column(Text)
    ref_ids: Mapped[list] = mapped_column(JSON, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    review_status: Mapped[str] = mapped_column(String(30), default="unreviewed")
    review_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Snapshot(Base):
    __tablename__ = "qa_snapshots"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    narrative: Mapped[str | None] = mapped_column(Text)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    body: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="accepted")
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Artifact(Base):
    __tablename__ = "qa_artifacts"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    storage_key: Mapped[str] = mapped_column(String(300), unique=True)
    filename: Mapped[str] = mapped_column(String(250))
    media_type: Mapped[str] = mapped_column(String(150))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Report(Base):
    __tablename__ = "qa_reports"
    __table_args__ = (UniqueConstraint("snapshot_id", "format", "template_id", "template_version"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("qa_snapshots.id"))
    format: Mapped[str] = mapped_column(String(20))
    template_id: Mapped[str] = mapped_column(String(80))
    template_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default="queued")
    artifact_id: Mapped[str | None] = mapped_column(ForeignKey("qa_artifacts.id"))
    outdated: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "qa_jobs"
    __table_args__ = (UniqueConstraint("kind", "entity_id"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Asset(Base):
    __tablename__ = "qa_assets"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    owner_id: Mapped[str] = mapped_column(String(200), index=True)
    team_id: Mapped[str | None] = mapped_column(String(200))
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssetVersion(Base):
    __tablename__ = "qa_asset_versions"
    __table_args__ = (UniqueConstraint("asset_id", "version"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("qa_assets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    content_md: Mapped[str] = mapped_column(Text)
    ref_ids: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(100))
    applicability: Mapped[str] = mapped_column(Text)
    procedure: Mapped[dict | None] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    validation_status: Mapped[str] = mapped_column(String(40), default="unvalidated")
    validation_report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Feedback(Base):
    __tablename__ = "qa_feedback"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("qa_assets.id"), index=True)
    asset_version: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Idempotency(Base):
    __tablename__ = "qa_idempotency"
    __table_args__ = (UniqueConstraint("scope", "operation", "entity", "key"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = "qa_audit"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(200))
    actor_kind: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(80))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("qa_cases.id"), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class UITicket(Base):
    __tablename__ = "qa_ui_tickets"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    claims: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
