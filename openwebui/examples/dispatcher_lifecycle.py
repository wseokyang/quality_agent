"""Testable integration seam for a verified server-side dispatcher.

Not an automatically installed Open WebUI hook. IDs below must come from the
dispatcher's persisted logical dispatch record, validated within the current
response attempt. Model-supplied tool_call_id by itself is not trusted.
"""
from quality_bridge import BridgeClient, TraceContext, begin_run, close_run, execute_read


async def dispatch_one_read(*, user: dict, chat_id: str, request_id: str,
                            invocation_id: str, user_message: str,
                            initial_case_revision: int,
                            tool_id: str, tool_version: str, arguments: dict,
                            original_call, result_mapper):
    """One read per response attempt demo; a multi-call turn begins/closes once.

    Explicit new response attempt -> new persisted request_id.
    Retried delivery of the SAME dispatch -> SAME request_id/invocation_id.
    Separate intentional identical calls -> DIFFERENT invocation_id.
    """
    client = BridgeClient.for_user(user, "trace_adapter", chat_id)
    case = await client.current_case()
    if case is None:
        return await execute_read(client, tool_id=tool_id, tool_version=tool_version,
                                  arguments=arguments, invoke=original_call)
    # Persist the initial expected revision with the dispatch record too. A
    # delivery retry must replay the same semantic request, not a new revision.
    run = await begin_run(client, case, request_id=request_id, user_message=user_message,
                          expected_revision=initial_case_revision)
    context = TraceContext(case_id=case["id"], run_id=run["id"], request_id=request_id,
                           invocation_id=invocation_id, epoch=client.settings.epoch)
    scoped = client.scoped(case_id=case["id"], run_id=run["id"])
    result = await execute_read(scoped, tool_id=tool_id, tool_version=tool_version,
                               arguments=arguments, invoke=original_call,
                               result_mapper=result_mapper, context=context)
    # A duplicate can be an in-flight call; do not close it on another caller's
    # behalf. Incomplete delivery stays open for recovery and human review.
    if result.get("executed") and result.get("trace_complete"):
        await close_run(scoped, run["id"], "succeeded")
    return result
