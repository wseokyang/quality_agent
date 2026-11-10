"""
title: 품질 케이스와 검토 화면
author: Quality Team
description: 현재 대화의 품질 케이스를 만들고 인증된 검토 화면을 엽니다.
version: 0.1.0
"""
from html import escape
from urllib.parse import quote

from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from quality_bridge.client import BridgeClient, BridgeError, context_id, operation_key


class Action:
    class Valves(BaseModel):
        priority: int = 0

    def __init__(self):
        self.valves = self.Valves()
        self.actions = [
            {"id": "open", "name": "품질 검토 열기"},
            {"id": "create", "name": "품질 케이스 시작"},
        ]

    async def action(self, body: dict, __user__=None, __id__=None,
                     __event_emitter__=None, __event_call__=None):
        try:
            # __user__ is supplied by Open WebUI; body identity is ignored.
            chat_id = context_id(body.get("chat_id"), "chat_id")
            client = BridgeClient.for_user(__user__, "human", chat_id)
            case = await client.current_case()
            if __id__ == "create" and case is None:
                if not __event_call__:
                    raise BridgeError("INTERACTION_REQUIRED", "케이스 생성을 위한 입력 창을 사용할 수 없습니다.")
                title = await __event_call__({"type": "input", "data": {
                    "title": "품질 케이스 시작", "message": "이 대화에서 진행할 품질 업무의 제목을 입력하세요.",
                    "placeholder": "예: 협력사 검사 결과 검토",
                }})
                # Timeout/disconnected results are dict(error), never a title.
                if not isinstance(title, str) or not title.strip():
                    return None
                title = title.strip()
                if len(title) > 200:
                    raise BridgeError("INVALID_TITLE", "케이스 제목은 200자 이하로 입력하세요.")
                confirmed = await __event_call__({"type": "confirmation", "data": {
                    "title": "관리 케이스로 등록", "message": f"‘{title}’ 케이스를 만들까요? 이후 관리 대상 Tool은 검증된 실행 기록 연결이 필요합니다.",
                }})
                if confirmed is not True:
                    return None
                case = await client.request("POST", "/cases", body={
                    "title": title, "purpose": title, "profile_id": "quality-general", "profile_version": 1,
                }, key=operation_key("case", client.settings.instance_id, client.user_id, chat_id, title))
            elif __id__ not in {"open", "create", None}:
                raise BridgeError("INVALID_ACTION", "등록되지 않은 작업입니다.")
            scoped = client.scoped(case_id=case["id"]) if case else client
            ticket = await scoped.request("POST", "/ui/tickets", body={})
            link = client.settings.ui_base_url + "/ui/index.html#ticket=" + quote(ticket["ticket"], safe="")
            label = escape(case.get("title", "품질 업무") if case else "품질 업무")
            return HTMLResponse(content=f"""<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="referrer" content="no-referrer">
<style>body{{font:15px system-ui,sans-serif;padding:16px;color:#15334c;background:#f3f7fa}}a{{display:inline-block;padding:12px 18px;background:#155e75;color:white;border-radius:8px;text-decoration:none}}p{{line-height:1.6}}</style>
</head><body><strong>{label}</strong><p>근거 검토 · 결과 확정 · 보고서 · 절차와 지식</p>
<a href="{escape(link, quote=True)}" target="_blank" rel="noopener noreferrer">품질 검토 화면 열기</a>
<p>일회용 연결입니다. 만료되면 대화의 ‘품질 검토 열기’를 다시 누르세요.</p>
<script>parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*');</script>
</body></html>""", headers={"Content-Disposition": "inline", "Cache-Control": "no-store"})
        except BridgeError as exc:
            if __event_emitter__:
                await __event_emitter__({"type": "notification", "data": {"type": "error", "content": str(exc)}})
            return HTMLResponse(content=f"<p>{escape(str(exc))}</p>", headers={"Content-Disposition": "inline", "Cache-Control": "no-store"})
