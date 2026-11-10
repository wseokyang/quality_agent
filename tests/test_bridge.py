"""Boundary tests: real original callable, fake HTTP, JWT and plugin signatures."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re
import sys

import httpx
import jwt
import pytest

OPENWEBUI = Path(__file__).resolve().parents[1] / "openwebui"
sys.path.insert(0, str(OPENWEBUI))
from quality_bridge import BridgeClient, BridgeError, BridgeSettings, TraceContext, execute_read, verified_user, begin_run, close_run
from quality_bridge.outbox import OutcomeOutbox
from quality_bridge.drafting import draft_evidence


def plugin(name):
    spec = importlib.util.spec_from_file_location(name, OPENWEBUI / "plugins" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def settings(**kwargs):
    return BridgeSettings(
        api_base_url="http://quality-api:8080", ui_base_url="https://quality.example.test",
        instance_id="qrew-test", secrets={"human": "h" * 40, "model_delegate": "m" * 40, "trace_adapter": "t" * 40},
        **kwargs,
    )


def decode(request):
    token = request.headers["Authorization"].removeprefix("Bearer ")
    kid = jwt.get_unverified_header(token)["kid"]
    return jwt.decode(token, settings().secrets[kid], algorithms=["HS256"],
                      audience="quality-api", issuer="quality-openwebui")


def response(data, status=200):
    return httpx.Response(status, json={"data": data, "request_id": "api-test"})


CASE = {"id": "case-1", "title": "검사 결과", "revision": 1, "lifecycle": "active"}
CTX = TraceContext(case_id="case-1", run_id="run-1", request_id="request-1", invocation_id="invoke-1", epoch="1")


@pytest.mark.parametrize("user", [None, {}, {"id": "alice", "role": "pending"}, {"id": "", "role": "user"}])
def test_pending_missing_identity_rejected(user):
    with pytest.raises(BridgeError):
        verified_user(user)


def test_jwt_binds_user_instance_role_and_scope_without_keys_in_body():
    requests = []
    def handle(request):
        requests.append(request)
        return response({"ok": True})
    client = BridgeClient(settings(), "alice", "trace_adapter", "chat-1", "case-1", "run-1", httpx.MockTransport(handle))
    asyncio.run(client.request("GET", "/runs/run-1"))
    claims = decode(requests[0])
    assert claims["kind"] == "trace_adapter"
    assert claims["user_id"] == "alice" and claims["case_id"] == "case-1" and claims["run_id"] == "run-1"
    assert claims["instance_id"] == "qrew-test" and claims["epoch"] == "1"
    assert claims["exp"] - claims["iat"] == 60
    assert not requests[0].content
    assert "t" * 40 not in repr(client)


@pytest.mark.parametrize("configured,expected", [
    ("http://quality-api:8080", "http://quality-api:8080/quality/v1/context/current"),
    ("http://quality-api:8080/quality/v1/", "http://quality-api:8080/quality/v1/context/current"),
    ("https://quality.example.test/service", "https://quality.example.test/service/quality/v1/context/current"),
    ("https://quality.example.test/service/quality/v1", "https://quality.example.test/service/quality/v1/context/current"),
])
def test_api_base_accepts_origin_and_legacy_prefixed_path(configured, expected):
    urls = []
    def handle(request):
        urls.append(str(request.url))
        return response({"case": None, "run_id": None})
    cfg = BridgeSettings(api_base_url=configured, ui_base_url="https://quality.example.test",
                         instance_id="qrew-test", secrets=settings().secrets)
    client = BridgeClient(cfg, "alice", "trace_adapter", "chat-1", transport=httpx.MockTransport(handle))
    assert asyncio.run(client.current_case()) is None
    assert urls == [expected]


def test_general_chat_original_result_preserved():
    count = []
    async def source():
        count.append(1)
        return {"unchanged": [1, 2]}
    def handle(request):
        assert request.url.path.endswith("/context/current")
        return response({"case": None, "run_id": None})
    client = BridgeClient(settings(), "alice", "trace_adapter", "chat-1", transport=httpx.MockTransport(handle))
    actual = asyncio.run(execute_read(client, tool_id="existing", tool_version="1", arguments={}, invoke=source))
    assert actual == {"unchanged": [1, 2]} and len(count) == 1


def test_managed_case_missing_context_never_calls_original():
    called = []
    def handle(request):
        return response({"case": CASE, "run_id": "run-1"})
    client = BridgeClient(settings(), "alice", "trace_adapter", "chat-1", transport=httpx.MockTransport(handle))
    with pytest.raises(BridgeError, match="호출 ID"):
        asyncio.run(execute_read(client, tool_id="lookup", tool_version="1", arguments={}, invoke=lambda: called.append(1)))
    assert not called


def test_claim_then_source_then_outcome_and_duplicate_never_reexecutes():
    events, claims = [], []
    def handle(request):
        path = request.url.path
        if path.endswith("/context/current"):
            return response({"case": CASE, "run_id": "run-1"})
        if path.endswith("/calls/claim"):
            events.append("claim")
            claims.append(json.loads(request.content))
            if len(claims) == 1:
                return response({"call_id": "call-1", "execute": True, "claim_token": "claim-token"})
            return response({"call_id": "call-1", "execute": False, "state": "succeeded", "result_ref_ids": ["ref-1"]})
        assert path.endswith("/calls/call-1/outcome")
        events.append("outcome")
        assert decode(request)["run_id"] == "run-1"
        payload = json.loads(request.content)
        assert payload["result"] == {"count": 2}
        return response({"result_ref_ids": ["ref-1"]})
    def source():
        events.append("source")
        return {"count": 2, "internal_secret": "must-not-be-recorded"}
    client = BridgeClient(settings(), "alice", "trace_adapter", "chat-1", transport=httpx.MockTransport(handle))
    async def run():
        kwargs = dict(tool_id="lookup", tool_version="1", arguments={"id": "sample"}, invoke=source,
                      context=CTX, result_mapper=lambda value: {"count": value["count"]})
        first = await execute_read(client, **kwargs)
        second = await execute_read(client, **kwargs)
        return first, second
    first, second = asyncio.run(run())
    assert events == ["claim", "source", "outcome", "claim"]
    assert first["source_ref_ids"] == ["ref-1"] and first["trace_complete"]
    assert not second["executed"] and "claim_token" not in first


def test_failed_claim_never_calls_source_or_leaks_upstream_response():
    called = []
    def handle(request):
        if request.url.path.endswith("/context/current"):
            return response({"case": CASE, "run_id": "run-1"})
        return httpx.Response(403, json={"error": {"code": "EFFECT_BLOCKED", "message": "secret upstream debug"}})
    client = BridgeClient(settings(), "alice", "trace_adapter", "chat-1", transport=httpx.MockTransport(handle))
    with pytest.raises(BridgeError) as error:
        asyncio.run(execute_read(client, tool_id="write", tool_version="1", arguments={}, invoke=lambda: called.append(1), context=CTX))
    assert error.value.code == "EFFECT_BLOCKED" and "secret" not in str(error.value)
    assert called == []


def test_outbox_retries_only_delivery_and_keeps_no_signing_keys(tmp_path):
    count, attempts = [], []
    def handle(request):
        if request.url.path.endswith("/context/current"):
            return response({"case": CASE, "run_id": "run-1"})
        if request.url.path.endswith("/calls/claim"):
            return response({"call_id": "call-1", "execute": True, "claim_token": "claim-token"})
        attempts.append(json.loads(request.content))
        if len(attempts) == 1:
            raise httpx.ReadTimeout("timeout", request=request)
        return response({"result_ref_ids": ["ref-1"]})
    def source():
        count.append(1)
        return ["already-produced"]
    cfg = settings(outbox_path=str(tmp_path / "outbox.sqlite"))
    client = BridgeClient(cfg, "alice", "trace_adapter", "chat-1", transport=httpx.MockTransport(handle))
    result = asyncio.run(execute_read(client, tool_id="read", tool_version="1", arguments={}, invoke=source, context=CTX))
    assert result["state"] == "recording_pending" and not result["trace_complete"]
    db_bytes = (tmp_path / "outbox.sqlite").read_bytes()
    for secret in cfg.secrets.values():
        assert secret.encode() not in db_bytes
    delivered = asyncio.run(OutcomeOutbox(cfg.outbox_path).deliver(client))
    assert delivered["delivered"] == 1 and len(count) == 1
    assert attempts[0] == attempts[1]


def test_source_exception_records_unknown_not_false_failure():
    outcomes = []
    def handle(request):
        if request.url.path.endswith("/context/current"):
            return response({"case": CASE, "run_id": "run-1"})
        if request.url.path.endswith("/calls/claim"):
            return response({"call_id": "call-1", "execute": True, "claim_token": "claim-token"})
        outcomes.append(json.loads(request.content))
        return response({"result_ref_ids": []})
    def source():
        raise TimeoutError("Bearer source-secret")
    client = BridgeClient(settings(), "alice", "trace_adapter", "chat-1", transport=httpx.MockTransport(handle))
    actual = asyncio.run(execute_read(client, tool_id="read", tool_version="1", arguments={}, invoke=source, context=CTX))
    assert actual["state"] == "unknown" and outcomes[0]["error_code"] == "SOURCE_RESULT_UNKNOWN"
    assert "source-secret" not in json.dumps(actual)


def test_action_creates_case_with_human_confirmation_and_public_ticket(monkeypatch):
    module = plugin("quality_action")
    paths, interactions = [], []
    def handle(request):
        paths.append(request.url.path)
        assert decode(request)["kind"] == "human"
        if request.url.path.endswith("/cases") and request.method == "GET":
            return response({"items": []})
        if request.url.path.endswith("/cases"):
            assert json.loads(request.content)["title"] == "협력사 검토"
            return response(CASE)
        assert request.url.path.endswith("/ui/tickets")
        assert decode(request)["case_id"] == "case-1"
        return response({"ticket": "one-use-ticket"})
    client = BridgeClient(settings(), "alice", "human", "chat-1", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(module.BridgeClient, "for_user", lambda *args: client)
    async def interact(event):
        interactions.append(event["type"])
        return "협력사 검토" if event["type"] == "input" else True
    rendered = asyncio.run(module.Action().action({"chat_id": "chat-1"}, __user__={"id": "alice", "role": "user"},
                                                __id__="create", __event_call__=interact))
    assert interactions == ["input", "confirmation"]
    assert b"https://quality.example.test/ui/index.html#ticket=one-use-ticket" in rendered.body
    assert b"quality-api:8080" not in rendered.body and b"Bearer" not in rendered.body


def test_action_disconnected_input_never_creates_case(monkeypatch):
    module = plugin("quality_action")
    requests = []
    def handle(request):
        requests.append(request.method)
        return response({"items": []})
    client = BridgeClient(settings(), "alice", "human", "chat-1", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(module.BridgeClient, "for_user", lambda *args: client)
    async def disconnect(event):
        return {"error": "timeout"}
    assert asyncio.run(module.Action().action({"chat_id": "chat-1"}, __id__="create", __event_call__=disconnect)) is None
    assert requests == ["GET"]


def test_model_plugin_exposes_no_approval_or_generic_http():
    tool = plugin("quality_tools").Tools()
    exposed = {name for name in dir(tool) if name.startswith("quality_") and callable(getattr(tool, name))}
    assert exposed == {"quality_case_context", "quality_propose_evidence", "quality_draft_knowledge",
                       "quality_draft_procedure", "quality_search_assets"}


def test_model_proposal_uses_server_ref_run_and_correct_endpoint(monkeypatch):
    module = plugin("quality_tools")
    posted = []
    def handle(request):
        assert decode(request)["kind"] == "model_delegate"
        path = request.url.path
        if path.endswith("/context/current"):
            return response({"case": CASE, "run_id": "run-2"})
        if path.endswith("/cases/case-1"):
            return response({**CASE, "refs": [{"id": "ref-1", "run_id": "run-1"}]})
        assert path.endswith("/cases/case-1/evidence/proposals")
        assert decode(request)["run_id"] == "run-1"
        posted.append(json.loads(request.content))
        return response({"id": "ev-1", "review_status": "unreviewed"})
    client = BridgeClient(settings(), "alice", "model_delegate", "chat-1", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(module.BridgeClient, "for_user", lambda *args: client)
    result = asyncio.run(module.Tools().quality_propose_evidence(["ref-1"], "실제 데이터", __chat_id__="chat-1", __message_id__="message-1"))
    assert result["review_status"] == "unreviewed"
    assert posted == [{"ref_ids": ["ref-1"], "summary": "실제 데이터", "interpretation": None}]


def test_plugins_and_real_api_create_trace_propose_and_ticket_exchange(client, monkeypatch):
    """Exercise real auth/router/schema contracts, not only mocked path names."""
    backend = client.app.state.settings
    cfg = BridgeSettings(api_base_url="http://quality.test", ui_base_url="https://quality.test",
                         instance_id="test-instance", secrets=backend.signing_keys,
                         epoch=backend.recovery_epoch)
    transport = httpx.ASGITransport(app=client.app)
    action_module, tools_module = plugin("quality_action"), plugin("quality_tools")
    def for_user(user, kind, chat_id):
        return BridgeClient(cfg, verified_user(user), kind, chat_id, transport=transport)
    monkeypatch.setattr(BridgeClient, "for_user", for_user)
    user = {"id": "alice", "role": "user"}
    calls = []
    async def interact(event):
        return "협력사 검토" if event["type"] == "input" else True
    async def source():
        calls.append(1)
        return {"defect_count": 2, "unit": "count"}
    async def scenario():
        html = await action_module.Action().action({"chat_id": "chat-1"}, __user__=user,
                                                   __id__="create", __event_call__=interact)
        ticket = re.search(r"#ticket=([^\"]+)", html.body.decode())[1]
        human = for_user(user, "human", "chat-1")
        case = await human.current_case()
        trace = for_user(user, "trace_adapter", "chat-1")
        discovered = await trace.current_case()
        assert discovered["id"] == case["id"]
        run = await begin_run(trace, discovered, request_id="req-api-1", user_message="검사 결과를 조회해줘")
        context = TraceContext(case_id=case["id"], run_id=run["id"], request_id="req-api-1",
                               invocation_id="invocation-api-1", epoch=cfg.epoch)
        kwargs = dict(tool_id="fixture_read", tool_version="1", arguments={}, invoke=source, context=context)
        outcome = await execute_read(trace, **kwargs)
        duplicate = await execute_read(trace, **kwargs)
        assert outcome["trace_complete"] and not duplicate["executed"]
        await close_run(trace.scoped(case_id=case["id"]), run["id"])
        tool = tools_module.Tools()
        proposal = await tool.quality_propose_evidence(outcome["source_ref_ids"], "불량 건수는 2건이다.",
                                                     __user__=user, __chat_id__="chat-1", __message_id__="msg-api-1")
        assert "error" not in proposal, proposal
        draft = await tool.quality_draft_knowledge("검토 메모", "조회 기준", "조회한 기준과 결과", outcome["source_ref_ids"],
                                                  "이 조회 범위", __user__=user, __chat_id__="chat-1", __message_id__="msg-api-1")
        assert "error" not in draft, draft
        procedure = await tool.quality_draft_procedure(run["id"], "조회 절차", "확인된 조회", "동일 입력 조건",
                                                      __user__=user, __chat_id__="chat-1", __message_id__="msg-api-1")
        assert "error" not in procedure, procedure
        return ticket
    ticket = asyncio.run(scenario())
    assert calls == [1]
    assert client.post("/quality/v1/ui/exchange", json={"ticket": ticket}).status_code == 200
    assert client.post("/quality/v1/ui/exchange", json={"ticket": ticket}).status_code == 401


STORED_REFS = [{"id": "ref-stored", "content": {"count": 2}, "provenance_status": "observed"}]


def draft_proposal(**updates):
    return {"kind": "proposal", "proposal": {"ref_ids": ["ref-stored"], "summary": "2건 확인",
                                                "interpretation": None, **updates}, "question": None}


def test_draft_format_repair_once_uses_identical_stored_input_and_does_not_mutate():
    refs, original, seen = deepcopy(STORED_REFS), deepcopy(STORED_REFS), []
    async def model_only(request):
        seen.append(request)
        local_copy = json.loads(request.source_refs_json)
        local_copy[0]["content"]["count"] = 999
        return "```json\ninvalid\n```" if request.attempt == 1 else draft_proposal()
    result = asyncio.run(draft_evidence(refs, model_only))
    assert len(seen) == 2 and seen[0].source_refs_json == seen[1].source_refs_json
    assert seen[0].user_task == seen[1].user_task
    assert seen[1].validation_errors == ("INVALID_JSON",)
    assert result["status"] == "proposal" and result["evidence_review_required"]
    assert refs == original == result["raw_refs"]
    result["raw_refs"][0]["content"]["count"] = 500
    assert refs == original


@pytest.mark.parametrize("bad", [
    draft_proposal(ref_ids=["ref-not-allowed"]),
    draft_proposal(approved=True),
    draft_proposal(metrics={"count": 2}),
    draft_proposal(evidence_id="invented"),
    draft_proposal(interpretation={"approved": True}),
    {**draft_proposal(), "owner_id": "someone-else"},
])
def test_draft_policy_violations_immediately_require_manual_review(bad):
    calls = []
    async def model_only(request):
        calls.append(request)
        return bad
    result = asyncio.run(draft_evidence(STORED_REFS, model_only))
    assert len(calls) == 1 and result["attempts"] == 1
    assert result["status"] == "manual_review_required" and result["proposal"] is None
    assert result["manual_review_required"] and result["raw_refs"] == STORED_REFS
    assert result["error_code"] in {"UNALLOWED_FIELDS", "UNALLOWED_SOURCE_REF"}


@pytest.mark.parametrize("bad", [
    draft_proposal(summary=""), draft_proposal(summary="a" * 1001),
    draft_proposal(interpretation="a" * 2001), draft_proposal(ref_ids=[]),
    draft_proposal(ref_ids=["ref-stored", "ref-stored"]),
    {"kind": "proposal", "proposal": {"ref_ids": ["ref-stored"], "summary": "필드 누락"}, "question": None},
    {"kind": "needs_input", "proposal": None, "question": "a" * 1001},
])
def test_draft_repeated_schema_error_stops_at_two_model_calls(bad):
    calls = []
    async def model_only(request):
        calls.append(request)
        return bad
    result = asyncio.run(draft_evidence(STORED_REFS, model_only))
    assert len(calls) == 2 and result["attempts"] == 2
    assert result["status"] == "manual_review_required" and result["raw_refs"] == STORED_REFS


def test_draft_needs_input_returns_question_without_creating_proposal_or_retry():
    calls = []
    async def model_only(request):
        calls.append(request)
        return {"kind": "needs_input", "proposal": None, "question": "비교 기준은 무엇인가요?"}
    result = asyncio.run(draft_evidence(STORED_REFS, model_only))
    assert len(calls) == 1 and result["status"] == "needs_input"
    assert result["question"] == "비교 기준은 무엇인가요?" and result["proposal"] is None


def test_draft_model_transport_error_has_no_repair_and_no_secret_leak():
    calls = []
    async def model_only(request):
        calls.append(request)
        raise RuntimeError("Authorization: Bearer private-provider-key")
    result = asyncio.run(draft_evidence(STORED_REFS, model_only))
    assert len(calls) == 1 and result["error_code"] == "MODEL_UNAVAILABLE"
    assert "private-provider-key" not in json.dumps(result)


def test_draft_model_timeout_is_bounded_and_not_retried():
    calls = []
    async def model_only(request):
        calls.append(request)
        await asyncio.sleep(5)
    result = asyncio.run(draft_evidence(STORED_REFS, model_only, timeout_seconds=0.01))
    assert len(calls) == 1 and result["manual_review_required"]


def test_draft_rejects_invalid_source_snapshot_before_model_invocation():
    calls = []
    async def model_only(request):
        calls.append(request)
    with pytest.raises(ValueError):
        asyncio.run(draft_evidence([{"id": "ref-stored", "content": float("nan")}], model_only))
    assert calls == []


def test_draft_callback_request_is_immutable():
    from dataclasses import FrozenInstanceError
    async def model_only(request):
        with pytest.raises(FrozenInstanceError):
            request.source_refs_json = "[]"
        return draft_proposal()
    assert asyncio.run(draft_evidence(STORED_REFS, model_only))["status"] == "proposal"
