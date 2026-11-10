"""Immutable snapshot reports and authorization-checked artifact delivery."""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
from pathlib import Path
from typing import Literal

from docx import Document
from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import select

from ..auth import Principal, require_kind
from ..common import audit, case_for, digest, idempotent, new_id, ok, utcnow
from ..errors import APIError
from ..models import Artifact, Audit, Case, Job, Report, Snapshot
from .core import Actor, DB, DTO, Key, Revision, get_ref, report_data

router = APIRouter()


class ReportCreate(DTO):
    format: Literal["md", "docx"]
    template_id: Literal["quality-standard"] = "quality-standard"
    template_version: Revision = 1


def verify_snapshot_sources(db, snapshot, p):
    for entry in snapshot.body.get("evidence_versions", []):
        for ref in entry.get("refs", []):
            current = get_ref(db, ref["id"], p, snapshot.case_id)
            if current.content_hash != ref["content_hash"]:
                raise APIError(409, "SOURCE_INVALID", "A snapshot source failed its integrity check")


@router.post("/snapshots/{snapshot_id}/reports")
def create_report(snapshot_id: str, body: ReportCreate, p: Actor, db: DB, key: Key):
    # Jobs are created by a verified human; workers process persisted jobs, not arbitrary payloads.
    require_kind(p, "human")
    snapshot = db.get(Snapshot, snapshot_id)
    if not snapshot:
        raise APIError(404, "NOT_FOUND", "Snapshot not found")
    case = case_for(db, snapshot.case_id, p, lock=True)
    db.refresh(snapshot)
    verify_snapshot_sources(db, snapshot, p)
    if body.template_version != 1:
        raise APIError(422, "TEMPLATE_UNSUPPORTED", "Only quality-standard version 1 is supported")

    def perform():
        previous = db.scalar(select(Report).where(
            Report.snapshot_id == snapshot.id, Report.format == body.format,
            Report.template_id == body.template_id, Report.template_version == body.template_version,
        ))
        if previous:
            job = db.scalar(select(Job).where(Job.kind == "report", Job.entity_id == previous.id))
            if previous.state in {"failed", "interrupted"}:
                previous.state = "queued"
                previous.error_code = None
                job.state = "queued"
                job.error_code = None
                job.finished_at = None
                audit(db, p, "report.retry_requested", previous.id, case.id)
            return report_data(previous) | {"report_id": previous.id, "job_id": job.id}
        report = Report(id=new_id("report"), case_id=case.id, snapshot_id=snapshot.id,
                        format=body.format, template_id=body.template_id,
                        template_version=body.template_version, state="queued",
                        outdated=snapshot.state != "accepted")
        db.add(report)
        db.flush()
        job = Job(id=new_id("job"), kind="report", entity_id=report.id, state="queued", attempts=0)
        db.add(job)
        db.flush()
        audit(db, p, "report.requested", report.id, case.id, {"snapshot_id": snapshot.id})
        return report_data(report) | {"report_id": report.id, "job_id": job.id}
    return ok(idempotent(db, p, "report.create", snapshot.id, key, body.model_dump(), perform))


@router.get("/reports/{report_id}")
def get_report(report_id: str, p: Actor, db: DB):
    require_kind(p, "human")
    report = db.get(Report, report_id)
    if not report:
        raise APIError(404, "NOT_FOUND", "Report not found")
    case_for(db, report.case_id, p)
    snapshot = db.get(Snapshot, report.snapshot_id)
    verify_snapshot_sources(db, snapshot, p)
    return ok(report_data(report))


@router.get("/artifacts/{artifact_id}/content")
def download_artifact(artifact_id: str, request: Request, p: Actor, db: DB):
    require_kind(p, "human")
    artifact = db.get(Artifact, artifact_id)
    if not artifact:
        raise APIError(404, "NOT_FOUND", "Artifact not found")
    case_for(db, artifact.case_id, p)
    report = db.scalar(select(Report).where(Report.artifact_id == artifact.id))
    if not report or report.state != "succeeded":
        raise APIError(404, "NOT_FOUND", "Artifact is unavailable")
    verify_snapshot_sources(db, db.get(Snapshot, report.snapshot_id), p)
    base = Path(request.app.state.settings.artifact_dir).resolve()
    path = (base / artifact.storage_key).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise APIError(404, "ARTIFACT_UNAVAILABLE", "Stored artifact is unavailable")
    # Bound memory and verify bytes before delivery; never send a corrupt backup as a report.
    if artifact.size_bytes > 32 * 1024 * 1024:
        raise APIError(422, "LIMIT_EXCEEDED", "Artifact exceeds the supported download size")
    data = path.read_bytes()
    if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise APIError(409, "ARTIFACT_INTEGRITY_FAILED", "Stored artifact failed its integrity check")
    audit(db, p, "artifact.downloaded", artifact.id, artifact.case_id)
    # Filename is generated by this service, never a caller-controlled filesystem name.
    return Response(content=data, media_type=artifact.media_type,
                    headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"',
                             "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def _safe_md(value):
    # Render user and model prose as literal text, not HTML or executable Markdown links.
    escaped = html.escape(str(value), quote=False)
    for char in ("\\", "`", "*", "_", "[", "]", "#", "!", "|", ">"):
        escaped = escaped.replace(char, "\\" + char)
    return escaped


def render_report(snapshot, report):
    """Render solely from the approved immutable snapshot; no model/source calls."""
    body = snapshot.body
    sections = [
        ("title", snapshot.title),
        ("paragraph", "사람이 채택한 근거로 승인한 결과 스냅샷입니다. 자연어 해석은 승인자의 검토 결과이며 자동 사실 인증이 아닙니다."),
        ("paragraph", f"Snapshot: {snapshot.id}\n승인자: {body['accepted_by']}\n승인 시각: {body['accepted_at']}\nProfile: {snapshot.profile['profile_id']} v{snapshot.profile['profile_version']}"),
    ]
    if snapshot.narrative:
        sections += [("heading", "결과 설명"), ("paragraph", snapshot.narrative)]
    for index, entry in enumerate(body["evidence_versions"], 1):
        sections += [("heading", f"근거 {index}: {entry['evidence_id']} v{entry['version']}"),
                     ("paragraph", entry["summary"])]
        if entry.get("interpretation"):
            sections += [("paragraph", "검토된 해석: " + entry["interpretation"])]
        for ref in entry["refs"]:
            sections += [("subheading", ref["label"]),
                         ("paragraph", f"출처: {ref['id']} / {ref['provenance_status']} / SHA256 {ref['content_hash']}"),
                         ("paragraph", "위치: " + _text(ref["locator"])),
                         ("source", _text(ref["content"]))]
    if report.format == "md":
        chunks = []
        for kind, value in sections:
            if kind in {"title", "heading", "subheading"}:
                prefix = {"title": "#", "heading": "##", "subheading": "###"}[kind]
                chunks.append(prefix + " " + _safe_md(value))
            elif kind == "source":
                # Longer than any backtick sequence in the stored source.
                max_ticks = max((len(match.group()) for match in re.finditer(r"`+", str(value))), default=3)
                fence = "`" * max(4, max_ticks + 1)
                chunks.append(f"{fence}text\n{value}\n{fence}")
            else:
                chunks.append(_safe_md(value))
        data = ("\n\n".join(chunks) + "\n").encode("utf-8")
        media = "text/markdown; charset=utf-8"
    else:
        document = Document()
        for kind, value in sections:
            # OOXML forbids control bytes except TAB/LF/CR.
            clean = "".join(ch for ch in str(value) if ord(ch) >= 32 or ch in "\n\t\r")
            if kind in {"title", "heading", "subheading"}:
                document.add_heading(clean, {"title": 0, "heading": 1, "subheading": 2}[kind])
            else:
                document.add_paragraph(clean)
        output = io.BytesIO()
        document.save(output)
        data = output.getvalue()
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Report exceeds artifact size limit")
    return data, media


def process_next_report(db, settings):
    """Process one durable job in caller transaction, using PostgreSQL SKIP LOCKED.

    A crash before transaction commit leaves the job queued for retry. Atomic rename
    may leave an unreferenced artifact on disk, never an approved database reference
    to a partial file. Report output is deterministic from the fixed snapshot.
    """
    candidate = db.scalar(select(Job).where(Job.kind == "report", Job.state == "queued")
                          .order_by(Job.created_at).limit(1))
    if not candidate:
        return False
    report = db.get(Report, candidate.entity_id)
    if report:
        # All mutations use case -> job lock order. Keep snapshot invalidation and
        # report publication atomic, including the current/outdated marker.
        locked_case = db.scalar(select(Case).where(Case.id == report.case_id)
                                .with_for_update(skip_locked=True))
        if not locked_case:
            return False
        db.refresh(report)
    job = db.scalar(select(Job).where(Job.id == candidate.id, Job.state == "queued")
                    .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if not job:
        return False
    job.state = "running"
    job.attempts += 1
    job.claimed_at = utcnow()
    temporary = None
    try:
        if not report:
            raise ValueError("Missing report")
        snapshot = db.get(Snapshot, report.snapshot_id)
        case = db.get(Case, report.case_id)
        if not snapshot or not case or case.deleted_at or snapshot.content_hash != digest(snapshot.body):
            raise ValueError("Snapshot unavailable or corrupt")
        origin_actor = Principal(user_id=snapshot.created_by, kind="worker", subject="quality-report-worker",
                                 instance_id=case.instance_id, chat_id=case.chat_id, case_id=case.id,
                                 team_ids=tuple(settings.user_teams.get(snapshot.created_by, [])),
                                 epoch=settings.recovery_epoch)
        verify_snapshot_sources(db, snapshot, origin_actor)
        data, media = render_report(snapshot, report)
        artifact_id = new_id("artifact")
        storage_key = f"reports/{artifact_id}.{report.format}"
        path = Path(settings.artifact_dir) / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # fsync directory entry before publishing its database reference.
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        artifact = Artifact(id=artifact_id, case_id=report.case_id, storage_key=storage_key,
                            filename=f"quality-report-{snapshot.id}.{report.format}", media_type=media,
                            sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        db.add(artifact)
        db.flush()
        report.artifact_id = artifact.id
        report.state = "succeeded"
        report.outdated = snapshot.state != "accepted"
        report.error_code = None
        job.state = "succeeded"
        job.error_code = None
    except (OSError, ValueError, KeyError, TypeError, APIError):
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)
        job.state = "failed"
        job.error_code = "REPORT_GENERATION_FAILED"
        if report:
            report.state = "failed"
            report.error_code = job.error_code
    job.finished_at = utcnow()
    db.add(Audit(id=new_id("audit"), actor_id="quality-report-worker", actor_kind="worker",
                 action="report." + job.state, entity_id=job.entity_id,
                 case_id=report.case_id if report else None,
                 details={"job_id": job.id, "attempt": job.attempts, "error_code": job.error_code}))
    return True
