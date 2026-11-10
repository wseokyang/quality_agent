"""Asset authorization, provenance, human approval and reuse regression tests."""
import io
import json
import uuid
import zipfile
from datetime import timedelta

import jwt
import pytest
from sqlalchemy import select

from quality_api.common import digest, new_id, utcnow
from quality_api.models import Asset, AssetVersion, Audit, Case, Run, SourceRef, ToolCall

API = "/quality/v1"


@pytest.fixture
def seeded(db_factory, settings, headers):
    settings.user_teams_json = json.dumps({"alice": ["quality"], "bob": ["quality"], "curator": ["quality"], "outsider": []})
    settings.curator_ids_json = '["curator"]'
    settings.tool_registry_json = json.dumps([{"id": "quality.search", "version": "1", "effect": "read", "shareable": True,
                                             "input_schema": {"type": "object", "properties": {"period": {"type": "string"}}, "required": ["period"], "additionalProperties": False}}])
    settings.tool_registry_path = None

    def make(shareable=True, owner="alice", team=None, with_run=False):
        authorization = headers(user_id=owner)["Authorization"]
        identity = jwt.decode(authorization.split(" ", 1)[1], options={"verify_signature": False})
        case_id, ref_id = new_id("case"), new_id("ref")
        run_id, call_id = new_id("run"), new_id("call")
        chat_id = new_id("chat")
        with db_factory() as db:
            case = Case(id=case_id, owner_id=owner, team_id=team, instance_id=identity["instance_id"],
                        chat_id=chat_id, title="품질 사례", purpose="자산 검토", profile={"id": "general", "version": 1})
            db.add(case)
            db.flush()
            if with_run:
                db.add(Run(id=run_id, case_id=case_id, request_id=new_id("req"), input_hash=digest("input"),
                           user_message="품질 조회", state="completed", trace_complete=True, profile={"id": "general", "version": 1}))
                db.flush()
                db.add(ToolCall(id=call_id, run_id=run_id, invocation_id=new_id("inv"), tool_id="quality.search", tool_version="1",
                                arguments={"period": "ORIGINAL_CONFIDENTIAL_PERIOD"}, args_hash=digest({"period": "ORIGINAL_CONFIDENTIAL_PERIOD"}),
                                state="succeeded", claim_token_hash="a" * 64, adapter_subject="test-adapter", epoch=settings.recovery_epoch,
                                result_ref_ids=[ref_id]))
                db.flush()
            db.add(SourceRef(id=ref_id, case_id=case_id, run_id=run_id if with_run else None, call_id=call_id if with_run else None,
                             provenance_status="tool_recorded" if with_run else "manual_unverified", label="참고 근거", locator={},
                             content={"finding": "SOURCE_RAW_DATA"}, content_hash=digest({"finding": "SOURCE_RAW_DATA"}), shareable=shareable))
            db.commit()
        return {"case_id": case_id, "chat_id": chat_id, "ref_id": ref_id, "run_id": run_id if with_run else None}
    return make


def draft_body(source, **changes):
    return {"kind": "knowledge", "title": "품질 조사 지식", "summary": "품질 조사 시 확인할 사항", "content_md": "조사 결과를 확인하고 출처를 기록한다.",
            "ref_ids": [source["ref_id"]], "category": "quality", "applicability": "해당 기준을 사용하는 품질 조사", "procedure": None, **changes}


def post(client, headers, path, body, *, user="alice", kind="human", case_id=None, chat_id="chat-1", key=None):
    return client.post(API + path, json=body, headers=headers(kind=kind, user_id=user, case_id=case_id, chat_id=chat_id, key=key or uuid.uuid4().hex))


def create(client, headers, source, **changes):
    response = post(client, headers, f'/cases/{source["case_id"]}/asset-drafts', draft_body(source, **changes))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def approve(client, headers, asset, **changes):
    response = post(client, headers, f'/assets/{asset["id"]}/approve',
                    {"expected_asset_revision": asset["revision"], "asset_version": asset["current_version"], "content_hash": asset["version"]["content_hash"], "expires_at": None, **changes})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def submit(client, headers, asset):
    response = post(client, headers, f'/assets/{asset["id"]}/submit',
                    {"expected_asset_revision": asset["revision"], "asset_version": asset["current_version"], "target_team_id": "quality"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def publish(client, headers, asset):
    response = post(client, headers, f'/assets/{asset["id"]}/curation-decisions',
                    {"expected_asset_revision": asset["revision"], "asset_version": asset["current_version"], "action": "publish", "reason": "출처와 적용 조건을 확인함"}, user="curator")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_model_draft_is_not_reusable_and_model_cannot_approve(client, seeded, headers):
    source = seeded()
    response = post(client, headers, f'/cases/{source["case_id"]}/asset-drafts', draft_body(source), kind="model_delegate", case_id=source["case_id"], chat_id=source["chat_id"])
    assert response.status_code == 200
    asset = response.json()["data"]
    model_headers = headers(kind="model_delegate", case_id=source["case_id"])
    assert client.get(API + f'/assets/{asset["id"]}', headers=model_headers).status_code == 404
    assert client.get(API + '/assets', headers=model_headers).json()["data"]["items"] == []
    response = post(client, headers, f'/assets/{asset["id"]}/approve', {"expected_asset_revision": 1, "asset_version": 1, "content_hash": asset["version"]["content_hash"]}, kind="model_delegate", case_id=source["case_id"])
    assert response.status_code == 403
    approve(client, headers, asset)
    # Approved knowledge can be reused from a later case without mutating its origin.
    other_case = seeded()
    response = client.get(API + f'/assets/{asset["id"]}', headers=headers(kind="model_delegate", case_id=other_case["case_id"]))
    assert response.status_code == 200
    assert response.json()["data"]["usable"] is True


def test_exact_hash_revision_and_immutable_version(client, seeded, headers, db_factory):
    source = seeded()
    asset = create(client, headers, source)
    wrong = post(client, headers, f'/assets/{asset["id"]}/approve', {"expected_asset_revision": 1, "asset_version": 1, "content_hash": "0" * 64})
    assert wrong.status_code == 409
    approval = {"expected_asset_revision": 1, "asset_version": 1, "content_hash": asset["version"]["content_hash"]}
    first = post(client, headers, f'/assets/{asset["id"]}/approve', approval, key="same-approval")
    again = post(client, headers, f'/assets/{asset["id"]}/approve', approval, key="same-approval")
    assert first.json()["data"] == again.json()["data"]
    conflict = post(client, headers, f'/assets/{asset["id"]}/approve', {**approval, "content_hash": "f" * 64}, key="same-approval")
    assert conflict.status_code == 409
    response = post(client, headers, f'/assets/{asset["id"]}/versions', draft_body(source, content_md="새로운 검토 내용", expected_asset_revision=2, expected_current_version=1))
    assert response.status_code == 200
    revised = response.json()["data"]
    assert revised["status"] == "draft" and revised["current_version"] == 2
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(kind="model_delegate", case_id=source["case_id"])).status_code == 404
    with db_factory() as db:
        old = db.scalar(select(AssetVersion).where(AssetVersion.asset_id == asset["id"], AssetVersion.version == 1))
        assert old.content_md == draft_body(source)["content_md"]
        assert old.content_hash == asset["version"]["content_hash"]


def test_share_private_case_derived_asset_only_and_recheck_shareability(client, seeded, headers, db_factory):
    source = seeded(shareable=True, team=None)
    asset = approve(client, headers, create(client, headers, source))
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(user_id="bob")).status_code == 404
    asset = submit(client, headers, asset)
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(user_id="bob")).status_code == 404
    asset = publish(client, headers, asset)
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(user_id="bob")).status_code == 200
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(user_id="outsider")).status_code == 404
    with db_factory() as db:
        assert db.get(Case, source["case_id"]).team_id is None
        ref = db.get(SourceRef, source["ref_id"])
        ref.shareable = False
        db.commit()
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(user_id="bob")).status_code == 403
    assert client.get(API + '/assets', headers=headers(user_id="bob")).json()["data"]["items"] == []


def test_restricted_source_cannot_be_shared(client, seeded, headers):
    source = seeded(shareable=False)
    asset = approve(client, headers, create(client, headers, source))
    response = post(client, headers, f'/assets/{asset["id"]}/submit', {"expected_asset_revision": asset["revision"], "asset_version": 1, "target_team_id": "quality"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SOURCE_NOT_SHAREABLE"


def test_expired_assets_disappear_from_reuse_and_can_be_archived(client, seeded, headers, db_factory):
    source = seeded()
    asset = publish(client, headers, submit(client, headers, approve(client, headers, create(client, headers, source))))
    with db_factory() as db:
        db.get(Asset, asset["id"]).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert client.get(API + '/assets', headers=headers()).json()["data"]["items"] == []
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(user_id="bob")).status_code == 404
    assert client.get(API + f'/assets/{asset["id"]}/export', headers=headers()).status_code == 409
    response = client.get(API + f'/assets/{asset["id"]}', headers=headers())
    assert response.json()["data"]["effective_status"] == "expired"
    response = post(client, headers, f'/assets/{asset["id"]}/lifecycle', {"expected_asset_revision": asset["revision"], "asset_version": 1, "action": "archive", "reason": "유효기간 종료"})
    assert response.status_code == 200 and response.json()["data"]["status"] == "archived"


def valid_procedure(mode="guided"):
    return {"mode": mode, "parameter_schema": {"type": "object", "properties": {"period": {"type": "string"}}, "required": ["period"], "additionalProperties": False},
            "tool_bindings": [{"binding_id": "search", "tool_id": "quality.search", "tool_version": "1"}],
            "steps": [{"step_id": "lookup", "binding_id": "search", "depends_on": [], "input_bindings": [{"target": "period", "from": "parameter", "key": "period"}], "required_output_refs": []}],
            "profile_id": "general", "profile_version": "1", "output_schema": {"type": "object", "properties": {}}, "validation_fixture_refs": []}


def test_structure_validation_never_certifies_execution(client, seeded, headers):
    source = seeded()
    asset = create(client, headers, source, kind="procedure", procedure=valid_procedure("validated"))
    response = post(client, headers, f'/assets/{asset["id"]}/validate', {"expected_asset_revision": 1, "asset_version": 1, "fixture_ids": []})
    assert response.status_code == 200
    checked = response.json()["data"]
    assert checked["version"]["validation_status"] == "structure_checked"
    assert checked["version"]["validation_report"]["validated"] is False
    assert checked["version"]["validation_report"]["execution"] == "not_performed"
    assert checked["support"]["automatic_execution"] is False
    response = post(client, headers, f'/assets/{asset["id"]}/approve', {"expected_asset_revision": checked["revision"], "asset_version": 1, "content_hash": checked["version"]["content_hash"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "EXECUTION_VALIDATION_UNAVAILABLE"


def test_procedure_rejects_remote_schemas_unregistered_tools_and_fake_fixtures(client, seeded, headers):
    source = seeded()
    procedure = valid_procedure()
    procedure["parameter_schema"] = {"$ref": "https://untrusted.example/schema"}
    response = post(client, headers, f'/cases/{source["case_id"]}/asset-drafts', draft_body(source, kind="procedure", procedure=procedure))
    assert response.status_code == 422
    procedure = valid_procedure()
    procedure["tool_bindings"][0]["tool_id"] = "unknown"
    response = post(client, headers, f'/cases/{source["case_id"]}/asset-drafts', draft_body(source, kind="procedure", procedure=procedure))
    assert response.status_code == 422
    procedure = valid_procedure()
    procedure["validation_fixture_refs"] = ["invented-fixture"]
    response = post(client, headers, f'/cases/{source["case_id"]}/asset-drafts', draft_body(source, kind="procedure", procedure=procedure))
    assert response.status_code == 422


def test_recorded_run_procedure_redacts_values_and_exports_guided_package(client, seeded, headers):
    source = seeded(with_run=True)
    body = {"run_id": source["run_id"], "title": "정기 품질 조회", "summary": "기록된 읽기 Tools를 활용하는 지침", "applicability": "월별 품질 조사"}
    response = post(client, headers, f'/cases/{source["case_id"]}/procedure-drafts', body)
    assert response.status_code == 200, response.text
    asset = response.json()["data"]
    assert "ORIGINAL_CONFIDENTIAL_PERIOD" not in json.dumps(asset)
    assert asset["version"]["procedure"]["parameter_schema"]["properties"]["step_1__period"] == {"type": "string"}
    assert asset["version"]["procedure"]["mode"] == "guided"
    assert client.get(API + f'/assets/{asset["id"]}/export', headers=headers()).status_code == 409
    asset = approve(client, headers, asset)
    response = client.get(API + f'/assets/{asset["id"]}/export', headers=headers())
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert set(bundle.namelist()) == {"SKILL.md", "manifest.json", "README.txt"}
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["source_run_ids"] == [source["run_id"]]
        assert manifest["support"]["execution_validation"] is False
        all_text = " ".join(bundle.read(name).decode() for name in bundle.namelist())
        assert "ORIGINAL_CONFIDENTIAL_PERIOD" not in all_text and "SOURCE_RAW_DATA" not in all_text


def test_human_curation_merge_creates_new_draft_preserves_originals(client, seeded, headers, db_factory):
    source = seeded()
    target = publish(client, headers, submit(client, headers, approve(client, headers, create(client, headers, source, content_md="기존 품질 기준"))))
    incoming = submit(client, headers, approve(client, headers, create(client, headers, source, content_md="보완할 품질 기준")))
    decision = {"expected_asset_revision": incoming["revision"], "asset_version": 1, "action": "propose_merge", "reason": "동일 기준의 보완", "merge_target_id": target["id"]}
    response = post(client, headers, f'/assets/{incoming["id"]}/curation-decisions', decision, user="curator")
    assert response.status_code == 200, response.text
    proposal = response.json()["data"]["proposal"]
    with db_factory() as db:
        proposal_audit = db.scalar(select(Audit).where(Audit.entity_id == incoming["id"], Audit.action == "asset.merge_proposed"))
        assert "content" not in proposal_audit.details
        assert "기존 품질 기준" not in json.dumps(proposal_audit.details, ensure_ascii=False)
        assert "보완할 품질 기준" not in json.dumps(proposal_audit.details, ensure_ascii=False)
    assert client.get(API + f'/assets/{target["id"]}', headers=headers()).json()["data"]["status"] == "published"
    confirmation = {name: proposal[name] for name in ["target_asset_id", "target_asset_version", "source_asset_versions", "proposed_content_hash", "expected_target_revision", "expected_source_revisions"]}
    denied = post(client, headers, f'/assets/{incoming["id"]}/merge-confirmations', confirmation, user="bob")
    assert denied.status_code in {403, 404}
    bad = post(client, headers, f'/assets/{incoming["id"]}/merge-confirmations', {**confirmation, "proposed_content_hash": "0" * 64}, user="curator")
    assert bad.status_code == 409
    response = post(client, headers, f'/assets/{incoming["id"]}/merge-confirmations', confirmation, user="curator")
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["target"]["status"] == "draft" and result["target"]["current_version"] == 2
    assert result["source"]["status"] == "deprecated"
    assert "보완할 품질 기준" in result["target"]["version"]["content_md"]
    with db_factory() as db:
        old = db.scalar(select(AssetVersion).where(AssetVersion.asset_id == target["id"], AssetVersion.version == 1))
        assert old.content_md == "기존 품질 기준"


def test_feedback_requires_current_approved_version_and_error_explanation(client, seeded, headers):
    source = seeded()
    asset = approve(client, headers, create(client, headers, source))
    path = f'/assets/{asset["id"]}/feedback'
    assert post(client, headers, path, {"asset_version": 1, "kind": "incorrect"}).status_code == 422
    assert post(client, headers, path, {"asset_version": 99, "kind": "useful"}).status_code == 409
    response = post(client, headers, path, {"asset_version": 1, "kind": "incorrect", "comment": "이전 문서 버전에서는 적용할 수 없음"})
    assert response.status_code == 200
    reviewed = client.get(API + f'/assets/{asset["id"]}', headers=headers()).json()["data"]
    assert reviewed["feedback"][0]["comment"] == "이전 문서 버전에서는 적용할 수 없음"


def test_source_acl_revocation_blocks_reuse_and_export(client, seeded, headers, db_factory):
    source = seeded()
    asset = approve(client, headers, create(client, headers, source))
    with db_factory() as db:
        db.get(Case, source["case_id"]).owner_id = "outsider"
        db.commit()
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers(kind="model_delegate", case_id=source["case_id"])).status_code == 403
    assert client.get(API + f'/assets/{asset["id"]}/export', headers=headers()).status_code == 403
    assert client.get(API + '/assets', headers=headers()).json()["data"]["items"] == []


def test_tool_contract_change_blocks_procedure_approval(client, seeded, headers, settings):
    source = seeded()
    asset = create(client, headers, source, kind="procedure", procedure=valid_procedure())
    registry = json.loads(settings.tool_registry_json)
    registry[0]["version"] = "2"
    settings.tool_registry_json = json.dumps(registry)
    response = post(client, headers, f'/assets/{asset["id"]}/approve', {"expected_asset_revision": 1, "asset_version": 1, "content_hash": asset["version"]["content_hash"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNREGISTERED_PROCEDURE_TOOL"


def test_idempotent_replay_does_not_restore_revoked_source_access(client, seeded, headers, db_factory):
    source = seeded()
    asset = create(client, headers, source)
    body = {"expected_asset_revision": 1, "asset_version": 1, "content_hash": asset["version"]["content_hash"]}
    response = post(client, headers, f'/assets/{asset["id"]}/approve', body, key="revoked-replay")
    assert response.status_code == 200
    with db_factory() as db:
        db.get(Case, source["case_id"]).owner_id = "outsider"
        db.commit()
    response = post(client, headers, f'/assets/{asset["id"]}/approve', body, key="revoked-replay")
    assert response.status_code == 403
    assert "content_md" not in response.text


def test_imported_source_chain_cannot_hide_origin_revocation(client, seeded, headers, db_factory):
    origin, imported = seeded(), seeded()
    with db_factory() as db:
        db.get(SourceRef, imported["ref_id"]).locator = {"imported_from_ref_id": origin["ref_id"]}
        db.commit()
    asset = approve(client, headers, create(client, headers, imported))
    with db_factory() as db:
        db.get(Case, origin["case_id"]).owner_id = "outsider"
        db.commit()
    assert client.get(API + f'/assets/{asset["id"]}', headers=headers()).status_code == 403
    assert client.get(API + '/assets', headers=headers()).json()["data"]["items"] == []


def test_replaying_old_version_mutations_checks_original_sources_after_new_version(client, seeded, headers, db_factory):
    origin, imported, accessible = seeded(), seeded(), seeded()
    with db_factory() as db:
        db.get(SourceRef, imported["ref_id"]).locator = {"imported_from_ref_id": origin["ref_id"]}
        db.commit()
    asset = create(client, headers, imported, content_md="V1_PRIVATE_IMPORTED_CONTENT")
    replay_requests = []
    def recorded(action, body, user="alice"):
        nonlocal asset
        path = f'/assets/{asset["id"]}/{action}'
        key = "historical-" + action
        response = post(client, headers, path, body, user=user, key=key)
        assert response.status_code == 200, response.text
        replay_requests.append((path, body, user, key))
        asset = response.json()["data"]
    recorded("validate", {"expected_asset_revision": asset["revision"], "asset_version": 1, "fixture_ids": []})
    recorded("approve", {"expected_asset_revision": asset["revision"], "asset_version": 1, "content_hash": asset["version"]["content_hash"]})
    feedback_body = {"asset_version": 1, "kind": "incorrect", "comment": "V1_PRIVATE_FEEDBACK_DETAILS"}
    feedback = post(client, headers, f'/assets/{asset["id"]}/feedback', feedback_body, key="historical-feedback")
    assert feedback.status_code == 200
    replay_requests.append((f'/assets/{asset["id"]}/feedback', feedback_body, "alice", "historical-feedback"))
    recorded("submit", {"expected_asset_revision": asset["revision"], "asset_version": 1, "target_team_id": "quality"})
    recorded("curation-decisions", {"expected_asset_revision": asset["revision"], "asset_version": 1, "action": "publish", "reason": "검토 완료"}, user="curator")
    recorded("lifecycle", {"expected_asset_revision": asset["revision"], "asset_version": 1, "action": "deprecate", "reason": "개정 준비"})
    revision = post(client, headers, f'/assets/{asset["id"]}/versions', draft_body(accessible, content_md="V2_CURRENT_ACCESSIBLE_CONTENT", expected_asset_revision=asset["revision"], expected_current_version=1))
    assert revision.status_code == 200, revision.text
    current = publish(client, headers, submit(client, headers, approve(client, headers, revision.json()["data"])))
    assert current["current_version"] == 2
    with db_factory() as db:
        db.get(Case, origin["case_id"]).owner_id = "outsider"
        db.commit()
    current_response = client.get(API + f'/assets/{asset["id"]}', headers=headers())
    assert current_response.status_code == 200
    assert current_response.json()["data"]["feedback"] == []
    assert "V1_PRIVATE_FEEDBACK_DETAILS" not in current_response.text
    for path, body, user, key in replay_requests:
        response = post(client, headers, path, body, user=user, key=key)
        assert response.status_code == 403, (path, response.text)
        assert "V1_PRIVATE_IMPORTED_CONTENT" not in response.text
        assert "V1_PRIVATE_FEEDBACK_DETAILS" not in response.text


def test_cached_merge_proposal_and_nested_result_recheck_historical_target_sources(client, seeded, headers, db_factory):
    origin, imported, source_ref, clean = seeded(), seeded(team="quality"), seeded(team="quality"), seeded()
    with db_factory() as db:
        db.get(SourceRef, imported["ref_id"]).locator = {"imported_from_ref_id": origin["ref_id"]}
        db.commit()
    target = publish(client, headers, submit(client, headers, approve(client, headers, create(client, headers, imported, content_md="PRIVATE_HISTORICAL_MERGE_TARGET"))))
    source = submit(client, headers, approve(client, headers, create(client, headers, source_ref)))
    propose_path = f'/assets/{source["id"]}/curation-decisions'
    proposal_body = {"expected_asset_revision": source["revision"], "asset_version": 1, "action": "propose_merge", "reason": "중복 검토", "merge_target_id": target["id"]}
    response = post(client, headers, propose_path, proposal_body, user="curator", key="old-merge-proposal")
    assert response.status_code == 200, response.text
    proposal = response.json()["data"]["proposal"]
    confirm_body = {name: proposal[name] for name in ["target_asset_id", "target_asset_version", "source_asset_versions", "proposed_content_hash", "expected_target_revision", "expected_source_revisions"]}
    confirm_path = f'/assets/{source["id"]}/merge-confirmations'
    response = post(client, headers, confirm_path, confirm_body, user="curator", key="old-merge-confirmation")
    assert response.status_code == 200, response.text
    merged = response.json()["data"]
    for asset in [merged["target"], merged["source"]]:
        revised = post(client, headers, f'/assets/{asset["id"]}/versions', draft_body(clean, content_md="CLEAN_CURRENT_CONTENT", expected_asset_revision=asset["revision"], expected_current_version=asset["current_version"]))
        assert revised.status_code == 200, revised.text
        publish(client, headers, submit(client, headers, approve(client, headers, revised.json()["data"])))
    with db_factory() as db:
        db.get(Case, origin["case_id"]).owner_id = "outsider"
        db.commit()
    for asset_id in [source["id"], target["id"]]:
        assert client.get(API + f'/assets/{asset_id}', headers=headers(user_id="curator")).status_code == 200
    for path, body, key in [(propose_path, proposal_body, "old-merge-proposal"), (confirm_path, confirm_body, "old-merge-confirmation")]:
        replay = post(client, headers, path, body, user="curator", key=key)
        assert replay.status_code == 403, replay.text
        assert "PRIVATE_HISTORICAL_MERGE_TARGET" not in replay.text


def test_same_user_from_another_openwebui_instance_cannot_reuse_assets(client, seeded, headers):
    source = seeded()
    asset = approve(client, headers, create(client, headers, source))
    for stage in ("personal", "published"):
        if stage == "published":
            asset = publish(client, headers, submit(client, headers, asset))
        for kind in ("human", "model_delegate"):
            foreign = headers(kind=kind, user_id="alice", case_id=source["case_id"], instance_id="another-openwebui-instance")
            assert client.get(API + f'/assets/{asset["id"]}', headers=foreign).status_code == 403
            assert client.get(API + f'/assets/{asset["id"]}/export', headers=foreign).status_code == 403
            assert client.get(API + '/assets', headers=foreign).json()["data"]["items"] == []
