"""Insertion helper for an EXISTING tool's actual source invocation.

This file does not invent your private endpoint, OAuth flow, or Tool body.
Keep those inside original_call. Only trusted dispatcher code may construct
TraceContext. No ID is extracted from a user prompt or tool arguments.
"""
from typing import Callable, Any

from quality_bridge import BridgeClient, TraceContext, execute_read


async def recorded_existing_lookup(*, original_call: Callable[[], Any],
                                   business_arguments: dict,
                                   business_result_mapper: Callable[[Any], Any],
                                   openwebui_user: dict, openwebui_chat_id: str,
                                   dispatcher_context: TraceContext | None):
    """Call at the original Tool boundary; not a model-visible Tool method.

    Example in your installed Tools.lookup_quality_records method:

        async def original_call():
            return await self._existing_lookup(lot_id, __oauth_token__)

        context = getattr(__request__.state, 'quality_trace_context', None)
        # Your VERIFIED dispatcher must install the per-invocation context.
        # Default absence intentionally blocks managed source execution.
        return await recorded_existing_lookup(
            original_call=original_call,
            business_arguments={'lot_id': lot_id},
            business_result_mapper=lambda rows: [
                {'lot_id': r['lot_id'], 'defect_count': r['defect_count']}
                for r in rows
            ],
            openwebui_user=__user__, openwebui_chat_id=__chat_id__,
            dispatcher_context=context,
        )

    Replace the fixed registry ID/version below with the reviewed existing
    tool's ID/version and exact schema. Never pass credentials as arguments.
    For parallel dispatch, pass an invocation-local ContextVar/context argument,
    NOT one shared mutable request.state value. This example's request.state
    lookup is only for a serial dispatcher whose identity semantics are tested.
    """
    client = BridgeClient.for_user(openwebui_user, "trace_adapter", openwebui_chat_id)
    return await execute_read(
        client, tool_id="quality.existing_lookup", tool_version="1",
        arguments=business_arguments, invoke=original_call,
        result_mapper=business_result_mapper, context=dispatcher_context,
    )
