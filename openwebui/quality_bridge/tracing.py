"""Tracing at the real tool dispatch boundary, not inferred from chat text.

The dispatcher is responsible for stable request and invocation IDs, lifecycle
begin/close and calling this wrapper for every managed source tool. Missing
trusted dispatcher context is a hard failure for managed cases. General chats
continue to invoke the original function unchanged.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
from typing import Any, Callable

from .client import BridgeClient, BridgeError, canonical, context_id, operation_key
from .outbox import OutcomeOutbox


@dataclass(frozen=True)
class TraceContext:
    case_id: str
    run_id: str
    request_id: str
    invocation_id: str
    epoch: str

    def __post_init__(self):
        for field in ("case_id", "run_id", "request_id", "invocation_id"):
            context_id(getattr(self, field), field)
        if not isinstance(self.epoch, str) or not self.epoch or len(self.epoch) > 80:
            raise BridgeError("CONTEXT_MISSING", "검증된 복구 세대가 필요합니다.")


async def _invoke_once(invoke: Callable[[], Any]) -> Any:
    # A closure preserves the original user's auth and arguments. Do not put
    # this function inside a retry. Synchronous functions run outside the loop.
    if inspect.iscoroutinefunction(invoke):
        return await invoke()
    value = await asyncio.to_thread(invoke)
    return await value if inspect.isawaitable(value) else value


async def begin_run(client: BridgeClient, case: dict, *, request_id: str,
                    user_message: str, input_ref_ids: list[str] | None = None,
                    expected_revision: int | None = None) -> dict:
    if client.kind != "trace_adapter":
        raise BridgeError("FORBIDDEN", "실행 시작에는 기록 어댑터가 필요합니다.")
    context_id(request_id, "request_id")
    return await client.scoped(case_id=case["id"]).request("POST", f"/cases/{case['id']}/runs", body={
        "request_id": request_id, "input_ref_ids": input_ref_ids or [],
        "user_message": user_message,
        "expected_revision": case["revision"] if expected_revision is None else expected_revision,
    }, key=operation_key("run", request_id))


async def close_run(client: BridgeClient, run_id: str, state: str = "succeeded") -> dict:
    if state not in {"succeeded", "failed", "interrupted", "unknown"}:
        raise BridgeError("INVALID_STATE", "허용된 실행 종료 상태가 아닙니다.")
    return await client.scoped(run_id=run_id).request("POST", f"/runs/{context_id(run_id, 'run_id')}/close",
        body={"state": state, "reason": None}, key=operation_key("close", run_id, state))


async def execute_read(client: BridgeClient, *, tool_id: str, tool_version: str,
                       arguments: dict, invoke: Callable[[], Any],
                       context: TraceContext | None = None,
                       result_mapper: Callable[[Any], Any] | None = None) -> Any:
    """Run the original function once after the API grants a source-read claim.

    Returned managed payload contains recorded refs for evidence proposals.
    No case -> exact original return value. Already-claimed -> status only.
    The mapper must whitelist nonsecret business result fields; it is installed
    server code, not model-authored code. Binary/file returns need a real mapper.
    """
    if client.kind != "trace_adapter":
        raise BridgeError("FORBIDDEN", "원천 실행 기록에는 기록 어댑터가 필요합니다.")
    case = await client.current_case()
    if case is None:
        return await _invoke_once(invoke)
    if context is None:
        raise BridgeError("TRACE_CONTEXT_REQUIRED", "관리 케이스의 신뢰된 호출 ID가 없습니다. 원천 API는 실행하지 않았습니다.")
    if case["id"] != context.case_id or context.epoch != client.settings.epoch:
        raise BridgeError("TRACE_CONTEXT_MISMATCH", "케이스 또는 복구 세대가 일치하지 않아 원천 API를 실행하지 않았습니다.")
    if case.get("lifecycle") != "active":
        raise BridgeError("INVALID_STATE", "진행 중인 케이스에서만 원천 조회를 실행할 수 있습니다.")
    scoped = client.scoped(case_id=context.case_id, run_id=context.run_id)
    claim = await scoped.request("POST", f"/runs/{context.run_id}/calls/claim", body={
        "invocation_id": context.invocation_id, "tool_id": tool_id,
        "tool_version": tool_version, "arguments": arguments, "expected_run_state": "running",
    }, key=operation_key("claim", context.run_id, context.invocation_id))
    if claim.get("execute") is not True:
        return {"executed": False, "call_id": claim.get("call_id"), "state": claim.get("state"),
                "source_ref_ids": claim.get("result_ref_ids", []),
                "message": "이미 접수된 호출입니다. 원천 API를 다시 실행하지 않았습니다."}
    call_id = context_id(claim["call_id"], "call_id")
    # Construct/verify persistent storage BEFORE the source call.
    outbox = OutcomeOutbox(scoped.settings.outbox_path) if scoped.settings.outbox_path else None
    original_result, source_error = None, None
    try:
        original_result = await _invoke_once(invoke)
    except (Exception, asyncio.CancelledError) as exc:
        source_error = exc
    # An exception does not prove nonexecution: unknown until explicit review.
    if source_error is not None:
        payload = {"claim_token": claim["claim_token"], "state": "unknown", "result": None,
                   "upstream_request_id": None, "error_code": "SOURCE_RESULT_UNKNOWN"}
    else:
        try:
            result = result_mapper(original_result) if result_mapper else original_result
            if result is None:
                raise BridgeError("RESULT_NOT_JSON", "None은 성공 증빙이 아닙니다. 빈 결과라면 []를 반환하세요.")
            if len(canonical(result).encode()) > 500_000:
                raise BridgeError("RESULT_TOO_LARGE", "원천 결과는 최대 500KB입니다. 전용 결과 어댑터가 필요합니다.")
        except Exception:
            payload = {"claim_token": claim["claim_token"], "state": "unknown", "result": None,
                       "upstream_request_id": None, "error_code": "RESULT_ADAPTER_REQUIRED"}
        else:
            payload = {"claim_token": claim["claim_token"], "state": "succeeded", "result": result,
                       "upstream_request_id": None, "error_code": None}
    if outbox:
        # Power-loss safe write BEFORE delivery. It can contain sensitive result
        # data; the CDEP operator must persist and restrict this directory.
        outbox.put(scoped, call_id, payload)
    try:
        outcome = await scoped.request("POST", f"/calls/{call_id}/outcome", body=payload, key=f"outcome:{call_id}")
    except BridgeError:
        if isinstance(source_error, asyncio.CancelledError):
            raise source_error
        return {"executed": True, "call_id": call_id, "trace_complete": False,
                "state": "recording_pending" if outbox else "unknown",
                "source_reexecuted": False, "message": "원천 호출 후 기록 전송에 실패했습니다. 검토 화면에서 복구 상태를 확인하세요."}
    if outbox:
        outbox.delete(call_id)
    if isinstance(source_error, asyncio.CancelledError):
        raise source_error
    return {"executed": True, "call_id": call_id, "trace_complete": payload["state"] == "succeeded",
            "state": payload["state"], "source_ref_ids": outcome.get("result_ref_ids", []),
            "result": payload["result"], "error_code": payload["error_code"]}
