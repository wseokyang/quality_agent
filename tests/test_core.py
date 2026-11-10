"""Behavior tests for ownership, claim replay, evidence invalidation and reports."""
from uuid import uuid4

from sqlalchemy import select

from quality_api.models import ToolCall
from quality_api.routes.reports import process_next_report

PREFIX = "/quality/v1"


def post(client, headers, path, body, **scope):
    return client.post(PREFIX + path, json=body, headers=headers(**scope))


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def case(client, headers, **scope):
    return data(post(client, headers, "/cases", {"title": "품질 문서 비교", "purpose": "개정 변경사항 확인"}, **scope))


def revision(client, headers, case_id):
    return data(client.get(PREFIX + f"/cases/{case_id}", headers=headers()))["revision"]


def manual_evidence(client, headers, c):
    imported = data(post(client, headers, f"/cases/{c['id']}/source-imports", {
        "kind": "user_text", "label": "사용자 제공 설명", "text": "개정 A의 기준은 3개 항목입니다.",
        "expected_revision": revision(client, headers, c["id"]),
    }))
    evidence = data(post(client, headers, f"/cases/{c['id']}/evidence/proposals", {
        "ref_ids": [imported["refs"][0]["id"]], "summary": "기준 3개 항목 확인", "interpretation": None,
    }))
    adopted = data(post(client, headers, f"/evidence/{evidence['id']}/reviews", {
        "evidence_version": 1, "decision": "adopted", "reason": None,
        "expected_revision": evidence["case_revision"],
    }))
    return imported["refs"][0], adopted


def accepted_snapshot(client, headers, c, evidence):
    return data(post(client, headers, f"/cases/{c['id']}/results", {
        "expected_revision": revision(client, headers, c["id"]),
        "evidence_versions": [{"evidence_id": evidence["id"], "version": evidence["version"]}],
        "title": "변경사항 검토 결과", "narrative": "검토 완료",
    }))


def run(client, headers, c):
    return data(post(client, headers, f"/cases/{c['id']}/runs", {
        "request_id": str(uuid4()), "input_ref_ids": [], "user_message": "품질 데이터 조회",
        "expected_revision": revision(client, headers, c["id"]),
    }, kind="trace_adapter", case_id=c["id"]))


def claim(client, headers, c, r, settings, key=None, invocation=None, **overrides):
    registered = next(item for item in settings.tool_registry.values() if item["effect"] == "read")
    schema = registered["input_schema"]
    arguments = {}
    # Fixture registry is intentionally simple; derive only primitive required properties.
    for name in schema.get("required", []):
        prop = schema["properties"][name]
        arguments[name] = prop.get("enum", [1 if prop.get("type") == "integer" else "sample"])[0]
    body = {"invocation_id": invocation or str(uuid4()), "tool_id": registered["id"],
            "tool_version": registered["version"], "arguments": arguments, "expected_run_state": "running"}
    body.update(overrides)
    response = post(client, headers, f"/runs/{r['id']}/calls/claim", body,
                    kind="trace_adapter", case_id=c["id"], run_id=r["id"], key=key)
    return response, body


def test_case_idempotency_and_unknown_fields(client, headers):
    key = str(uuid4())
    body = {"title": "Case", "purpose": "Purpose"}
    first = data(post(client, headers, "/cases", body, key=key))
    same = data(post(client, headers, "/cases", body, key=key))
    assert first["id"] == same["id"]
    assert post(client, headers, "/cases", body | {"title": "changed"}, key=key).status_code == 409
    assert post(client, headers, "/cases", body | {"owner_id": "bob"}).status_code == 422
    assert client.get(PREFIX + f"/cases/{first['id']}", headers=headers(user_id="bob")).status_code == 404


def test_claim_never_replays_execution_authority(client, headers, settings):
    c = case(client, headers)
    r = run(client, headers, c)
    key = str(uuid4())
    response, body = claim(client, headers, c, r, settings, key=key)
    first = data(response)
    assert first["execute"] is True and first["claim_token"]
    same = data(post(client, headers, f"/runs/{r['id']}/calls/claim", body,
                     kind="trace_adapter", case_id=c["id"], run_id=r["id"], key=key))
    assert same["execute"] is False and same["claim_token"] is None
    second_key = data(post(client, headers, f"/runs/{r['id']}/calls/claim", body,
                           kind="trace_adapter", case_id=c["id"], run_id=r["id"]))
    assert second_key["execute"] is False
    conflict = post(client, headers, f"/runs/{r['id']}/calls/claim", body | {"invocation_id": "different"},
                    kind="trace_adapter", case_id=c["id"], run_id=r["id"], key=key)
    assert conflict.status_code == 409


def test_outcome_idempotency_and_missing_result(client, headers, settings):
    c = case(client, headers)
    r = run(client, headers, c)
    response, _ = claim(client, headers, c, r, settings)
    call = data(response)
    scope = {"kind": "trace_adapter", "case_id": c["id"], "run_id": r["id"]}
    payload = {"claim_token": call["claim_token"], "state": "succeeded", "result": [],
               "upstream_request_id": "source-1", "error_code": None}
    assert post(client, headers, f"/calls/{call['id']}/outcome", payload | {"result": None}, **scope).status_code == 422
    first = data(post(client, headers, f"/calls/{call['id']}/outcome", payload, **scope))
    second = data(post(client, headers, f"/calls/{call['id']}/outcome", payload, **scope))
    assert first["result_ref_ids"] == second["result_ref_ids"]
    assert first["refs"][0]["provenance_status"] == "observed"
    assert post(client, headers, f"/calls/{call['id']}/outcome", payload | {"result": ["changed"]}, **scope).status_code == 409
    closed = data(post(client, headers, f"/runs/{r['id']}/close", {"state": "succeeded", "reason": None}, **scope))
    assert closed["trace_complete"] is True
    assert data(client.get(PREFIX + f"/cases/{c['id']}", headers=headers()))["active_run_id"] is None


def test_source_write_tool_blocked_before_claim(client, headers, settings):
    c = case(client, headers)
    r = run(client, headers, c)
    response, _ = claim(client, headers, c, r, settings, tool_id="unknown-tool")
    assert response.status_code == 503
    assert post(client, headers, f"/runs/{r['id']}/calls/claim", {
        "invocation_id": "x", "tool_id": "unregistered", "tool_version": "1", "arguments": {},
    }, kind="model_delegate", case_id=c["id"], run_id=r["id"]).status_code == 403


def test_unknown_read_requires_explicit_human_exclusion(client, headers, settings):
    c = case(client, headers)
    r = run(client, headers, c)
    response, _ = claim(client, headers, c, r, settings)
    call = data(response)
    scope = {"kind": "trace_adapter", "case_id": c["id"], "run_id": r["id"]}
    assert post(client, headers, f"/runs/{r['id']}/close", {"state": "succeeded"}, **scope).status_code == 409
    data(post(client, headers, f"/runs/{r['id']}/close", {"state": "unknown", "reason": "연결 끊김"}, **scope))
    blocked = post(client, headers, f"/cases/{c['id']}/runs", {
        "request_id": "new", "input_ref_ids": [], "user_message": "retry",
        "expected_revision": revision(client, headers, c["id"]),
    }, kind="trace_adapter", case_id=c["id"])
    assert blocked.status_code == 409
    resolved = data(post(client, headers, f"/runs/{r['id']}/resolve-read", {
        "action": "exclude_unresolved_read", "call_ids": [call["id"]], "reason": "조회 결과를 포기함",
        "expected_revision": revision(client, headers, c["id"]),
    }))
    assert resolved["state"] == "interrupted" and resolved["trace_complete"] is False
    record = data(client.get(PREFIX + f"/runs/{r['id']}", headers=headers()))
    assert record["calls"][0]["state"] == "unknown"
    assert record["calls"][0]["disposition"] == "excluded_by_human"


def test_evidence_version_change_invalidates_snapshot(client, headers):
    c = case(client, headers)
    ref, evidence = manual_evidence(client, headers, c)
    snapshot = accepted_snapshot(client, headers, c, evidence)
    version = data(post(client, headers, f"/evidence/{evidence['id']}/versions", {
        "expected_current_version": 1, "expected_revision": snapshot["case_revision"],
        "ref_ids": [ref["id"]], "summary": "수정한 근거", "interpretation": None,
    }))
    assert version["version"] == 2 and version["review_status"] == "unreviewed"
    stale = post(client, headers, f"/cases/{c['id']}/results", {
        "expected_revision": version["case_revision"], "evidence_versions": [{"evidence_id": evidence["id"], "version": 1}],
        "title": "과거 버전 채택 시도", "narrative": None,
    })
    assert stale.status_code == 409
    detail = data(client.get(PREFIX + f"/cases/{c['id']}", headers=headers()))
    assert detail["snapshots"][0]["state"] == "superseded"
    assert detail["snapshots"][0]["body"]["evidence_versions"][0]["version"] == 1


def test_completed_case_requires_reopen(client, headers):
    c = case(client, headers)
    ref, evidence = manual_evidence(client, headers, c)
    snapshot = accepted_snapshot(client, headers, c, evidence)
    completed = data(post(client, headers, f"/cases/{c['id']}/complete", {
        "snapshot_id": snapshot["id"], "expected_revision": snapshot["case_revision"],
    }))
    assert completed["lifecycle"] == "completed"
    proposal = {"ref_ids": [ref["id"]], "summary": "새 초안", "interpretation": None}
    assert post(client, headers, f"/cases/{c['id']}/evidence/proposals", proposal).status_code == 409
    reopened = data(post(client, headers, f"/cases/{c['id']}/reopen", {
        "reason": "추가 검토", "expected_revision": completed["revision"],
    }))
    assert reopened["lifecycle"] == "active"
    assert post(client, headers, f"/cases/{c['id']}/evidence/proposals", proposal).status_code == 200


def test_model_cannot_approve_and_ref_fabrication_is_rejected(client, headers):
    c = case(client, headers)
    ref, evidence = manual_evidence(client, headers, c)
    r = run(client, headers, c)
    review = {"evidence_version": 1, "decision": "adopted", "reason": None,
              "expected_revision": revision(client, headers, c["id"])}
    assert post(client, headers, f"/evidence/{evidence['id']}/reviews", review,
                kind="model_delegate", case_id=c["id"], run_id=r["id"]).status_code == 403
    assert post(client, headers, f"/cases/{c['id']}/evidence/proposals", {
        "ref_ids": ["fabricated"], "summary": "임의 주장", "interpretation": None,
    }, kind="model_delegate", case_id=c["id"], run_id=r["id"]).status_code == 404
    assert post(client, headers, f"/cases/{c['id']}/evidence/proposals", {
        "ref_ids": [ref["id"]], "summary": "주장", "interpretation": None, "adopted": True,
    }).status_code == 422


def test_reports_render_snapshot_and_recheck_acl(client, headers, db_factory, settings):
    c = case(client, headers)
    ref, evidence = manual_evidence(client, headers, c)
    snapshot = accepted_snapshot(client, headers, c, evidence)
    for format in ("md", "docx"):
        report = data(post(client, headers, f"/snapshots/{snapshot['id']}/reports", {
            "format": format, "template_id": "quality-standard", "template_version": 1,
        }))
        with db_factory() as db:
            with db.begin():
                assert process_next_report(db, settings)
        ready = data(client.get(PREFIX + f"/reports/{report['id']}", headers=headers()))
        assert ready["state"] == "succeeded"
        download = client.get(PREFIX + f"/artifacts/{ready['artifact_id']}/content", headers=headers())
        assert download.status_code == 200
        if format == "md":
            assert "기준 3개 항목 확인" in download.text
        else:
            assert download.content.startswith(b"PK")
        denied = client.get(PREFIX + f"/artifacts/{ready['artifact_id']}/content", headers=headers(user_id="bob"))
        assert denied.status_code == 404


def test_unconfigured_import_does_not_create_fake_source(client, headers):
    c = case(client, headers)
    result = post(client, headers, f"/cases/{c['id']}/source-imports", {
        "kind": "artifact", "artifact_id": "pretend-upload", "expected_revision": c["revision"],
    })
    assert result.status_code == 503 and result.json()["error"]["code"] == "NOT_CONFIGURED"
    assert not data(client.get(PREFIX + f"/cases/{c['id']}", headers=headers()))["refs"]


def test_manual_source_stays_user_provided_after_adoption(client, headers):
    c = case(client, headers)
    ref, evidence = manual_evidence(client, headers, c)
    snapshot = accepted_snapshot(client, headers, c, evidence)
    assert snapshot["body"]["evidence_versions"][0]["refs"][0]["provenance_status"] == "user_provided"


def test_trash_is_soft_delete_and_owner_can_restore(client, headers):
    c = case(client, headers)
    trashed = data(post(client, headers, f"/cases/{c['id']}/trash", {"expected_revision": c["revision"]}))
    assert client.get(PREFIX + f"/cases/{c['id']}", headers=headers()).status_code == 404
    restored = data(post(client, headers, f"/cases/{c['id']}/restore", {"expected_revision": trashed["revision"]}))
    assert restored["deleted_at"] is None
    assert client.get(PREFIX + f"/cases/{c['id']}", headers=headers()).status_code == 200


def test_registered_write_tool_never_creates_claim(client, headers, settings, db_factory):
    import json
    settings.tool_registry_json = json.dumps([
        {"id": "write_api", "version": "1", "effect": "write", "input_schema": {"type": "object"}},
    ])
    c = case(client, headers)
    r = run(client, headers, c)
    denied = post(client, headers, f"/runs/{r['id']}/calls/claim", {
        "invocation_id": "write-attempt", "tool_id": "write_api", "tool_version": "1", "arguments": {},
    }, kind="trace_adapter", case_id=c["id"], run_id=r["id"])
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "EFFECT_BLOCKED"
    with db_factory() as db:
        assert db.scalar(select(ToolCall).where(ToolCall.run_id == r["id"])) is None


def test_unknown_run_with_no_calls_can_be_explicitly_resolved(client, headers):
    c = case(client, headers)
    r = run(client, headers, c)
    data(post(client, headers, f"/runs/{r['id']}/close", {"state": "unknown", "reason": "중단됨"},
              kind="trace_adapter", case_id=c["id"], run_id=r["id"]))
    resolved = data(post(client, headers, f"/runs/{r['id']}/resolve-read", {
        "action": "exclude_unresolved_read", "call_ids": [], "reason": "실행 기록 없는 요청 포기",
        "expected_revision": revision(client, headers, c["id"]),
    }))
    assert resolved["state"] == "interrupted"


def test_reconciliation_does_not_grant_execution_or_rewrite_approval(client, headers, settings):
    c = case(client, headers)
    r = run(client, headers, c)
    response, _ = claim(client, headers, c, r, settings)
    call = data(response)
    scope = {"kind": "trace_adapter", "case_id": c["id"], "run_id": r["id"]}
    data(post(client, headers, f"/runs/{r['id']}/close", {"state": "unknown"}, **scope))
    payload = {"claim_token": call["claim_token"], "state": "succeeded", "result": {"count": 4},
               "upstream_request_id": "recovered-1", "error_code": None,
               "reconciliation_reason": "원천의 보존된 응답을 확보함"}
    settings.recovery_epoch = "restored-epoch"
    recovered = data(post(client, headers, f"/calls/{call['id']}/reconciliation", payload, **scope))
    assert recovered["state"] == "succeeded" and "execute" not in recovered
    assert recovered["refs"][0]["locator"]["reconciled"] is True
    closed = data(post(client, headers, f"/runs/{r['id']}/close", {"state": "succeeded"}, **scope))
    assert closed["trace_complete"] is True


def test_imported_source_revocation_blocks_snapshot_replay(client, headers):
    origin = case(client, headers)
    original_ref, _ = manual_evidence(client, headers, origin)
    destination = case(client, headers, chat_id="chat-2")
    imported = data(post(client, headers, f"/cases/{destination['id']}/source-imports", {
        "kind": "existing_ref", "ref_ids": [original_ref["id"]], "expected_revision": destination["revision"],
    }))
    proposed = data(post(client, headers, f"/cases/{destination['id']}/evidence/proposals", {
        "ref_ids": [imported["refs"][0]["id"]], "summary": "가져온 자료", "interpretation": None,
    }))
    adopted = data(post(client, headers, f"/evidence/{proposed['id']}/reviews", {
        "evidence_version": 1, "decision": "adopted", "reason": None, "expected_revision": proposed["case_revision"],
    }))
    key = str(uuid4())
    body = {"expected_revision": adopted["case_revision"], "evidence_versions": [{"evidence_id": adopted["id"], "version": 1}],
            "title": "가져온 자료 결과", "narrative": None}
    data(post(client, headers, f"/cases/{destination['id']}/results", body, key=key))
    data(post(client, headers, f"/cases/{origin['id']}/trash", {"expected_revision": revision(client, headers, origin["id"])}))
    replay = post(client, headers, f"/cases/{destination['id']}/results", body, key=key)
    assert replay.status_code == 404
    detail = data(client.get(PREFIX + f"/cases/{destination['id']}", headers=headers()))
    assert detail["refs"] == [] and detail["snapshots"] == []


def test_report_source_fence_contains_hostile_markdown(client, headers, db_factory, settings):
    c = case(client, headers)
    hostile = "before\n````   \n<script>alert('x')</script>\n[link](javascript:alert(1))\ninline ``````` ticks"
    imported = data(post(client, headers, f"/cases/{c['id']}/source-imports", {
        "kind": "user_text", "label": "보존할 원문", "text": hostile, "expected_revision": c["revision"],
    }))
    evidence = data(post(client, headers, f"/cases/{c['id']}/evidence/proposals", {
        "ref_ids": [imported["refs"][0]["id"]], "summary": "원문 확인", "interpretation": None,
    }))
    adopted = data(post(client, headers, f"/evidence/{evidence['id']}/reviews", {
        "evidence_version": 1, "decision": "adopted", "reason": None, "expected_revision": evidence["case_revision"],
    }))
    snapshot = accepted_snapshot(client, headers, c, adopted)
    report = data(post(client, headers, f"/snapshots/{snapshot['id']}/reports", {"format": "md"}))
    with db_factory() as db:
        with db.begin():
            assert process_next_report(db, settings)
    ready = data(client.get(PREFIX + f"/reports/{report['id']}", headers=headers()))
    content = client.get(PREFIX + f"/artifacts/{ready['artifact_id']}/content", headers=headers()).text
    assert "````````text\n" + hostile + "\n````````" in content
    assert content.count("````````") == 2
