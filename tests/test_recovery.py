"""Restore quarantine must revoke execution without replaying source operations."""
from __future__ import annotations

import hashlib
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from quality_api.cli import recover, worker
from quality_api.common import digest, new_id, utcnow
from quality_api.models import Audit, Case, Job, Report, Run, Snapshot, ToolCall, UITicket
from quality_api.routes import reports

from test_core import accepted_snapshot, case, claim, data, manual_evidence, post, run

PREFIX = "/quality/v1"


@pytest.fixture
def restored_ledger(client, headers, settings, database):
    """Mix approved history with in-flight records, as a restored DB could contain."""
    historical = case(client, headers)
    _, evidence = manual_evidence(client, headers, historical)
    snapshot = accepted_snapshot(client, headers, historical, evidence)
    generated = data(post(client, headers, f"/snapshots/{snapshot['id']}/reports", {"format": "md"}))
    with database.Session.begin() as db:
        assert reports.process_next_report(db, settings)
    generated = data(client.get(PREFIX + f"/reports/{generated['id']}", headers=headers()))
    completed = data(post(client, headers, f"/cases/{historical['id']}/complete", {
        "snapshot_id": snapshot["id"], "expected_revision": snapshot["case_revision"],
    }))
    queued = data(post(client, headers, f"/snapshots/{snapshot['id']}/reports", {"format": "docx"}))

    def active_headers(**kwargs):
        return headers(chat_id="chat-pending", **kwargs)

    active_case = case(client, active_headers)
    active_run = run(client, active_headers, active_case)
    response, claim_body = claim(client, active_headers, active_case, active_run, settings)
    active_call = data(response)
    old_dispatch_headers = active_headers(kind="trace_adapter", case_id=active_case["id"], run_id=active_run["id"])

    unused = data(client.post(PREFIX + "/ui/tickets", headers=headers()))["ticket"]
    consumed = data(client.post(PREFIX + "/ui/tickets", headers=headers()))["ticket"]
    data(client.post(PREFIX + "/ui/exchange", json={"ticket": consumed}))
    with database.Session.begin() as db:
        completed_run = Run(id=new_id("run"), case_id=historical["id"], request_id="historical-finished-request",
                            input_hash=digest("historical"), user_message="보존할 완료 실행", input_ref_ids=[],
                            state="succeeded", trace_complete=True, profile=historical["profile"], finished_at=utcnow())
        db.add(completed_run)
        db.flush()
        completed_call = ToolCall(id=new_id("call"), run_id=completed_run.id, invocation_id="finished-invocation",
                                  tool_id="fixture_read", tool_version="1", arguments={}, args_hash=digest({}),
                                  state="succeeded", claim_token_hash=digest("old-proof"), adapter_subject="quality:trace_adapter",
                                  epoch=settings.recovery_epoch, outcome_hash=digest({"result": []}),
                                  result_ref_ids=[], finished_at=utcnow())
        db.add(completed_call)
        running_job = Job(id=new_id("job"), kind="validation", entity_id="validation-running", state="running", attempts=1)
        processing_job = Job(id=new_id("job"), kind="validation", entity_id="validation-processing", state="processing", attempts=1)
        db.add_all([running_job, processing_job])
        db.flush()
        consumed_row = db.scalar(select(UITicket).where(UITicket.token_hash == hashlib.sha256(consumed.encode()).hexdigest()))
        values = {
            "historical_case": historical["id"], "historical_revision": completed["revision"],
            "snapshot": snapshot, "generated_report": generated,
            "completed_run": completed_run.id, "completed_call": completed_call.id,
            "queued_report": queued, "extra_job_ids": [running_job.id, processing_job.id],
            "active_case": active_case["id"], "active_run": active_run["id"], "active_call": active_call["id"],
            "unused_ticket": unused, "consumed_ticket": consumed, "consumed_at": consumed_row.consumed_at,
            "old_dispatch_headers": old_dispatch_headers, "claim_body": claim_body,
        }
    return values


def ledger_state(database):
    """Snapshot observable persistence so rejection must leave every row untouched."""
    with database.Session() as db:
        return {
            "runs": [(row.id, row.state, row.trace_complete) for row in db.scalars(select(Run).order_by(Run.id))],
            "calls": [(row.id, row.state, row.outcome_hash, row.epoch) for row in db.scalars(select(ToolCall).order_by(ToolCall.id))],
            "jobs": [(row.id, row.state, row.error_code) for row in db.scalars(select(Job).order_by(Job.id))],
            "reports": [(row.id, row.state, row.artifact_id, row.error_code) for row in db.scalars(select(Report).order_by(Report.id))],
            "tickets": [(row.id, row.consumed_at) for row in db.scalars(select(UITicket).order_by(UITicket.id))],
            "audits": [row.id for row in db.scalars(select(Audit).order_by(Audit.id))],
        }


def test_recover_requires_explicit_recovery_mode(database, settings, restored_ledger):
    before = ledger_state(database)
    settings.recovery_epoch = "new-but-mode-not-enabled"
    with pytest.raises(SystemExit, match="QUALITY_RECOVERY_MODE"):
        recover(database, settings)
    assert ledger_state(database) == before


def test_recover_rejects_existing_epoch_without_partial_changes(database, settings, restored_ledger):
    before = ledger_state(database)
    settings.recovery_mode = True
    with pytest.raises(SystemExit, match="NEW recovery epoch"):
        recover(database, settings)
    assert ledger_state(database) == before


def test_recover_quarantines_pending_work_and_preserves_approved_history(
    client, headers, database, settings, restored_ledger, monkeypatch,
):
    ledger = restored_ledger
    # Recovery must never process a report job or invoke any dispatcher. A spy on
    # the only executable worker path makes accidental job processing observable.
    report_processor = Mock(side_effect=AssertionError("Recovery must not execute jobs"))
    monkeypatch.setattr(reports, "process_next_report", report_processor)
    settings.recovery_mode = True
    settings.recovery_epoch = "new-restored-generation"
    assert recover(database, settings) == {"runs": 1, "calls": 1, "jobs": 3, "tickets": 1}
    report_processor.assert_not_called()

    with database.Session() as db:
        pending_run = db.get(Run, ledger["active_run"])
        assert pending_run.state == "unknown" and pending_run.trace_complete is False
        assert db.get(ToolCall, ledger["active_call"]).state == "unknown"
        assert db.get(Case, ledger["active_case"]).active_run_id == pending_run.id
        assert db.get(Job, ledger["queued_report"]["job_id"]).state == "interrupted"
        for job_id in ledger["extra_job_ids"]:
            job = db.get(Job, job_id)
            assert job.state == "interrupted" and job.error_code == "RESTORE_RECONCILIATION_REQUIRED"
        interrupted_report = db.get(Report, ledger["queued_report"]["id"])
        assert interrupted_report.state == "failed" and interrupted_report.error_code == "RESTORE_RECONCILIATION_REQUIRED"
        case_history = db.get(Case, ledger["historical_case"])
        assert case_history.lifecycle == "completed" and case_history.revision == ledger["historical_revision"]
        assert db.get(Run, ledger["completed_run"]).state == "succeeded"
        assert db.get(ToolCall, ledger["completed_call"]).state == "succeeded"
        historical_snapshot = db.get(Snapshot, ledger["snapshot"]["id"])
        assert historical_snapshot.content_hash == ledger["snapshot"]["content_hash"]
        assert historical_snapshot.body == ledger["snapshot"]["body"]
        completed_report = db.get(Report, ledger["generated_report"]["id"])
        assert completed_report.state == "succeeded"
        assert completed_report.artifact_id == ledger["generated_report"]["artifact_id"]
        consumed_row = db.scalar(select(UITicket).where(UITicket.token_hash == hashlib.sha256(ledger["consumed_ticket"].encode()).hexdigest()))
        assert consumed_row.consumed_at == ledger["consumed_at"]
        quarantine = db.scalar(select(Audit).where(Audit.action == "restore.quarantined"))
        assert quarantine.details["epoch"] == settings.recovery_epoch

    # Repeating quarantine records the operator event without resetting data or
    # granting execution a second time.
    assert recover(database, settings) == {"runs": 0, "calls": 0, "jobs": 0, "tickets": 0}
    assert client.post(PREFIX + "/ui/exchange", json={"ticket": ledger["unused_ticket"]}).status_code == 401
    assert client.get("/health/ready").status_code == 503
    assert post(client, headers, "/cases", {"title": "blocked", "purpose": "blocked"}).status_code == 503

    settings.recovery_mode = False
    old_claim = client.post(PREFIX + f"/runs/{ledger['active_run']}/calls/claim",
                            headers=ledger["old_dispatch_headers"], json=ledger["claim_body"])
    assert old_claim.status_code == 401
    fresh_claim = client.post(PREFIX + f"/runs/{ledger['active_run']}/calls/claim",
                              headers=headers(kind="trace_adapter", chat_id="chat-pending",
                                              case_id=ledger["active_case"], run_id=ledger["active_run"]),
                              json=ledger["claim_body"])
    if fresh_claim.status_code == 200:
        assert fresh_claim.json()["data"]["execute"] is False
        assert fresh_claim.json()["data"]["claim_token"] is None
    else:
        assert fresh_claim.status_code == 409
    report_processor.assert_not_called()
    download = client.get(PREFIX + f"/artifacts/{ledger['generated_report']['artifact_id']}/content", headers=headers())
    assert download.status_code == 200


@pytest.mark.parametrize("guard", ["recovery_mode", "draining"])
def test_worker_rejects_guarded_mode_before_processing(database, settings, restored_ledger, monkeypatch, guard):
    before = ledger_state(database)
    processor = Mock(side_effect=AssertionError("Guarded worker reached report processor"))
    monkeypatch.setattr(reports, "process_next_report", processor)
    setattr(settings, guard, True)
    with pytest.raises(SystemExit, match="disabled"):
        worker(database, settings, once=True)
    processor.assert_not_called()
    assert ledger_state(database) == before
