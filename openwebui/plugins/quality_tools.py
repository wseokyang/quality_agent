"""
title: 품질 업무 제안 도구
author: Quality Team
description: 기록된 근거로 초안을 제안하고 승인된 지식과 절차를 검색합니다. 사람의 승인 권한은 없습니다.
version: 0.1.0
"""
from pydantic import BaseModel

from quality_bridge.client import BridgeClient, BridgeError, context_id, operation_key, safe_error


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def _case(self, user, chat_id):
        client = BridgeClient.for_user(user, "model_delegate", context_id(chat_id, "chat_id"))
        case = await client.current_case()
        if not case:
            raise BridgeError("CASE_REQUIRED", "사용자가 ‘품질 케이스 시작’을 눌러 먼저 케이스를 만들어야 합니다.")
        return client.scoped(case_id=case["id"], run_id=case.get("context_run_id")), case

    async def quality_case_context(self, __user__: dict = None, __chat_id__: str = None) -> dict:
        """현재 대화의 케이스와 등록된 출처 ID, 근거, 실행 상태를 읽습니다. 없는 출처를 만들지 마세요."""
        try:
            client, case = await self._case(__user__, __chat_id__)
            return await client.request("GET", f"/cases/{case['id']}")
        except BridgeError as exc:
            return safe_error(exc)

    async def quality_propose_evidence(self, ref_ids: list[str], summary: str,
                                      interpretation: str = "", __user__: dict = None,
                                      __chat_id__: str = None, __message_id__: str = None) -> dict:
        """실제 등록된 출처를 근거 초안으로 제안합니다. 채택과 검토 완료는 사람이 결정합니다.
        :param ref_ids: 현재 케이스 조회 또는 기록된 Tool 결과에 나온 source_ref_ids.
        :param summary: 출처에서 확인되는 사실 요약. 최대 1000자.
        :param interpretation: 사실과 구분되는 해석. 없으면 빈 문자열.
        """
        try:
            client, case = await self._case(__user__, __chat_id__)
            detail = await client.request("GET", f"/cases/{case['id']}")
            refs = {row["id"]: row for row in detail.get("refs", [])}
            if not ref_ids or any(ref not in refs for ref in ref_ids):
                raise BridgeError("SOURCE_INVALID", "현재 케이스에서 확인한 출처 ID만 사용할 수 있습니다.")
            run_ids = {refs[ref].get("run_id") for ref in ref_ids if refs[ref].get("run_id")}
            if len(run_ids) > 1:
                raise BridgeError("SOURCE_SCOPE_MISMATCH", "다른 실행의 출처는 별도 근거로 제안하세요.")
            # A human-imported source can be reviewed manually even if no run
            # exists. Do not invent run context to make model proposals succeed.
            run_id = next(iter(run_ids), None) or detail.get("active_run_id")
            if not run_id:
                raise BridgeError("RUN_CONTEXT_REQUIRED", "모델 제안에 연결할 실행 기록이 없습니다. 검토 화면에서 직접 근거를 등록하세요.")
            payload = {"ref_ids": ref_ids, "summary": summary, "interpretation": interpretation or None}
            key = operation_key("evidence", context_id(__message_id__, "message_id"), payload)
            return await client.scoped(run_id=run_id).request("POST", f"/cases/{case['id']}/evidence/proposals", body=payload, key=key)
        except BridgeError as exc:
            return safe_error(exc)

    async def quality_draft_knowledge(self, title: str, summary: str, content_md: str,
                                     ref_ids: list[str], applicability: str,
                                     __user__: dict = None, __chat_id__: str = None,
                                     __message_id__: str = None) -> dict:
        """업무 지식의 비공개 초안을 제안합니다. 승인·공유·게시하지 않습니다.
        :param title: 지식 제목.
        :param summary: 짧은 요약.
        :param content_md: 출처와 해석을 구분한 본문.
        :param ref_ids: 현재 케이스에 등록된 출처 ID 목록.
        :param applicability: 이 지식이 적용되는 조건과 한계.
        """
        try:
            client, case = await self._case(__user__, __chat_id__)
            payload = {"kind": "knowledge", "title": title, "summary": summary,
                       "content_md": content_md, "ref_ids": ref_ids, "category": "quality",
                       "applicability": applicability, "proposed_expires_at": None, "procedure": None}
            return await client.request("POST", f"/cases/{case['id']}/asset-drafts", body=payload,
                key=operation_key("knowledge", context_id(__message_id__, "message_id"), payload))
        except BridgeError as exc:
            return safe_error(exc)

    async def quality_draft_procedure(self, run_id: str, title: str, summary: str,
                                     applicability: str, __user__: dict = None,
                                     __chat_id__: str = None, __message_id__: str = None) -> dict:
        """기록된 조회 실행으로 절차 초안을 생성합니다. 자동 실행 가능하다고 보장하지 않습니다.
        :param run_id: 현재 케이스 조회에서 확인한 실행 ID.
        :param title: 절차 제목.
        :param summary: 절차의 목적.
        :param applicability: 사용할 업무 조건과 제한.
        """
        try:
            client, case = await self._case(__user__, __chat_id__)
            context_id(run_id, "run_id")
            # The API verifies the selected run belongs to this scoped case.
            payload = {"run_id": run_id, "title": title, "summary": summary,
                       "category": "quality", "applicability": applicability}
            return await client.request("POST", f"/cases/{case['id']}/procedure-drafts", body=payload,
                key=operation_key("procedure", context_id(__message_id__, "message_id"), payload))
        except BridgeError as exc:
            return safe_error(exc)

    async def quality_search_assets(self, query: str, kind: str = "knowledge",
                                   __user__: dict = None, __chat_id__: str = None) -> dict:
        """권한이 있고 만료되지 않은 승인 지식 또는 게시된 절차를 검색합니다.
        :param query: 검색어.
        :param kind: knowledge 또는 procedure.
        """
        try:
            if kind not in {"knowledge", "procedure"}:
                raise BridgeError("INVALID_KIND", "knowledge 또는 procedure만 검색할 수 있습니다.")
            client, _ = await self._case(__user__, __chat_id__)
            return await client.request("GET", "/assets", params={"q": query, "kind": kind})
        except BridgeError as exc:
            return safe_error(exc)
