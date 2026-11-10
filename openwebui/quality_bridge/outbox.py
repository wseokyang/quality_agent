"""Durable delivery of already executed results; NEVER schedules source calls.

The local SQLite journal contains source results and claim tokens, so its
directory must be restricted and persistent. JWTs and signing keys are not
stored. Callers must re-authenticate with the same server identity and epoch.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3

from .client import BridgeClient, BridgeError, canonical


class OutcomeOutbox:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise BridgeError("OUTBOX_UNSAFE", "결과 보관소에 심볼릭 링크를 사용할 수 없습니다.")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS outcomes (call_id TEXT PRIMARY KEY, payload TEXT NOT NULL, scope TEXT NOT NULL)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def put(self, client: BridgeClient, call_id: str, payload: dict):
        scope = {"user_id": client.user_id, "instance_id": client.settings.instance_id,
                 "kind": client.kind, "chat_id": client.chat_id, "case_id": client.case_id,
                 "run_id": client.run_id, "epoch": client.settings.epoch}
        serialized, scope_json = canonical(payload), canonical(scope)
        with self._connect() as db:
            existing = db.execute("SELECT payload, scope FROM outcomes WHERE call_id=?", (call_id,)).fetchone()
            if existing and existing != (serialized, scope_json):
                raise BridgeError("OUTBOX_CONFLICT", "같은 호출에 다른 결과를 덮어쓸 수 없습니다.")
            db.execute("INSERT OR IGNORE INTO outcomes VALUES(?,?,?)", (call_id, serialized, scope_json))

    def delete(self, call_id: str):
        with self._connect() as db:
            db.execute("DELETE FROM outcomes WHERE call_id=?", (call_id,))

    async def deliver(self, client: BridgeClient) -> dict:
        """Retry result HTTP only. Old recovery epoch is quarantined in place."""
        if client.kind != "trace_adapter":
            raise BridgeError("FORBIDDEN", "결과 재전송은 기록 어댑터만 할 수 있습니다.")
        with self._connect() as db:
            rows = db.execute("SELECT call_id,payload,scope FROM outcomes ORDER BY call_id").fetchall()
        delivered, pending, stale = 0, 0, 0
        for call_id, raw, scope_raw in rows:
            scope = json.loads(scope_raw)
            if scope["instance_id"] != client.settings.instance_id or scope["user_id"] != client.user_id:
                continue
            if scope["epoch"] != client.settings.epoch:
                stale += 1
                continue
            scoped = replace(client, chat_id=scope["chat_id"], case_id=scope["case_id"], run_id=scope["run_id"])
            try:
                await scoped.request("POST", f"/calls/{call_id}/outcome", body=json.loads(raw), key=f"outcome:{call_id}")
            except BridgeError:
                pending += 1
            else:
                self.delete(call_id)
                delivered += 1
        return {"delivered": delivered, "pending": pending, "stale_epoch": stale}
